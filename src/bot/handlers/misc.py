import logging
from html import escape
from pathlib import Path

from pyrogram import filters as pf
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from src.bot.home import render_home
from src.bot.stats import render_stats
from src.bot.state import _pending
from src.config import settings
from src.dispatcher.digest_builder import send_digest
from src.dispatcher.sender import send_document, send_reply

log = logging.getLogger(__name__)


def _tail_lines(path: Path, n: int, block_size: int = 4096) -> list[str]:
    with path.open("rb") as f:
        f.seek(0, 2)
        size = f.tell()
        data = b""
        pos = size
        while pos > 0 and data.count(b"\n") <= n:
            read = min(block_size, pos)
            pos -= read
            f.seek(pos)
            data = f.read(read) + data
    text = data.decode("utf-8", errors="replace")
    return text.splitlines()[-n:]


# sendMessage rejects anything past 4096 characters, and one log line carrying a
# collector error can be several hundred on its own. Escaping inflates it further,
# so the budget is measured after escaping, not before.
_TELEGRAM_TEXT_LIMIT = 4096
_LOG_TAIL_LINES = 20
_LOG_HEADER_BUDGET = 120


def _fit_log_lines(lines: list[str]) -> list[str]:
    """Drop whole lines from the top until the escaped <pre> block fits. Oldest go
    first: in a log tail the newest line is the one being read — and if that line
    alone is too long (a traceback), it is cut rather than dropped, or the reply
    would come back empty."""
    budget = _TELEGRAM_TEXT_LIMIT - _LOG_HEADER_BUDGET
    while len(lines) > 1 and len(escape("\n".join(lines))) > budget:
        lines = lines[1:]
    if lines and len(escape(lines[0])) > budget:
        # Slice first — a megabyte-long line (one traceback, no newlines) would
        # take a million escape() passes to trim one character at a time, on the
        # single event loop that also runs the collectors.
        line = lines[0][:budget]
        while line and len(escape(line)) > budget - 1:
            line = line[:len(line) * (budget - 1) // len(escape(line))] or line[:-1]
        lines = [line + "…"]
    return lines


def register_misc_handlers(bot, admin_msg, admin_cb) -> None:

    @bot.on_message(pf.chat(settings.telegram_supergroup_id) & pf.pinned_message)
    async def delete_pin_service(_, message: Message) -> None:
        try:
            await message.delete()
            log.info("Deleted pin service message id=%d", message.id)
        except Exception as exc:
            log.warning("Failed to delete pin service message id=%d: %s", message.id, exc)

    @bot.on_callback_query(pf.regex(r"^noop$") & admin_cb)
    async def cb_noop(_, query: CallbackQuery) -> None:
        await query.answer()

    @bot.on_message(pf.command("digest") & admin_msg)
    async def cmd_digest(_, message: Message) -> None:
        await _run_digest(message)

    async def _run_digest(message: Message) -> None:
        log.info("Manual digest triggered by user")
        status_msg = await message.reply("⏳ Building digest...")

        async def update_status(text: str) -> None:
            try:
                await status_msg.edit_text(text)
            except Exception:
                pass

        result = await send_digest(status_fn=update_status)
        await status_msg.delete()
        if result is None:
            await message.reply("⏳ Digest is already building, please wait.")
        elif result:
            pass
        else:
            await message.reply("ℹ️ No new items.")

    @bot.on_message(pf.command("stats") & admin_msg)
    async def cmd_stats(_, message: Message) -> None:
        await send_reply(message.chat.id, await render_stats(), reply_to_message_id=message.id)

    @bot.on_callback_query(pf.regex(r"^home_stats$") & admin_cb)
    async def cb_home_stats(_, query: CallbackQuery) -> None:
        await query.answer()
        await send_reply(query.message.chat.id, await render_stats())

    @bot.on_callback_query(pf.regex(r"^home_logs$") & admin_cb)
    async def cb_home_logs(_, query: CallbackQuery) -> None:
        await query.answer()
        await _send_log_tail(query.message)

    @bot.on_message(pf.command("folder_audit") & admin_msg)
    async def cmd_folder_audit(_, message: Message) -> None:
        await send_reply(message.chat.id, await _render_folder_audit(), reply_to_message_id=message.id)

    @bot.on_message(pf.command("logs") & admin_msg)
    async def cmd_logs(_, message: Message) -> None:
        await _send_log_tail(message)

    async def _render_folder_audit() -> str:
        from src.collectors.folder_manager import SENTINEL_FOLDER, audit_folder, folder_channel_ids
        from src.db.models import get_sources_of_type

        try:
            in_folder = await folder_channel_ids()
        except Exception as exc:
            log.warning("Folder audit failed to read the folder: %s", exc)
            return f"⚠️ Could not read the <b>{SENTINEL_FOLDER}</b> folder: {escape(str(exc))}"
        if in_folder is None:
            return f"⚠️ No <b>{SENTINEL_FOLDER}</b> folder on the userbot account."

        sources = [dict(row) for row in await get_sources_of_type("telegram")]
        report = audit_folder(in_folder, sources)
        log.info("Folder audit: %d in folder, %d tracked, %d stale, %d missing, %d without chat_id",
                 report["in_folder"], report["tracked"], len(report["stale"]),
                 len(report["missing"]), len(report["unknown"]))

        lines = [f"<b>Folder audit</b> · {SENTINEL_FOLDER}",
                 f"<i>{report['in_folder']} in folder · {report['tracked']} tracked</i>"]
        if report["stale"]:
            lines.append(f"\n<b>In folder, not tracked</b> ({len(report['stale'])})")
            lines.append("<i>a source was removed while its channel was renamed — "
                         "the userbot may still be a member</i>")
            lines += [f"· <code>-100{cid}</code>" for cid in report["stale"][:20]]
        if report["missing"]:
            lines.append(f"\n<b>Tracked, not in folder</b> ({len(report['missing'])})")
            lines += [f"· {escape(s['name'])} <i>{s['status']}</i>" for s in report["missing"][:20]]
        if report["unknown"]:
            lines.append(f"\n<b>No chat_id stored</b> ({len(report['unknown'])})")
            lines.append("<i>never resolved, so membership cannot be checked</i>")
            lines += [f"· {escape(s['name'])}" for s in report["unknown"][:20]]
        if not (report["stale"] or report["missing"] or report["unknown"]):
            lines.append("\n✅ Folder and sources agree.")
        return "\n".join(lines)

    async def _send_log_tail(message: Message) -> None:
        log_file = Path(settings.database_path).parent / "logs" / "sentinel.log"
        if not log_file.exists():
            await message.reply("📄 <b>Log</b>\n\nNo log file yet.")
            return
        tail = _fit_log_lines(_tail_lines(log_file, _LOG_TAIL_LINES))
        text = (
            f"📄 <b>Log</b> · last {len(tail)} lines\n"
            "<pre>" + escape("\n".join(tail)) + "</pre>"
        )
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("📥 Download full log", callback_data="logs_download")]])
        await message.reply(text, reply_markup=kb)

    @bot.on_callback_query(pf.regex(r"^logs_download$") & admin_cb)
    async def cb_logs_download(_, query: CallbackQuery) -> None:
        log_file = Path(settings.database_path).parent / "logs" / "sentinel.log"
        if not log_file.exists():
            await query.answer("Log file not found.", show_alert=True)
            return
        await query.answer()
        try:
            await send_document(query.message.chat.id, str(log_file), filename="sentinel.log")
        except Exception as exc:
            log.exception("Log download failed: %s", exc)
            await query.message.reply(f"Failed to send log file: {exc}")

    @bot.on_message(pf.command(["start", "home"]) & admin_msg)
    async def cmd_start(_, message: Message) -> None:
        text, kb = await render_home()
        await message.reply(text, reply_markup=kb)

    @bot.on_callback_query(pf.regex(r"^home$") & admin_cb)
    async def cb_home(_, query: CallbackQuery) -> None:
        _pending.pop(query.from_user.id, None)
        text, kb = await render_home()
        await query.message.edit_text(text, reply_markup=kb)

    @bot.on_callback_query(pf.regex(r"^home_digest$") & admin_cb)
    async def cb_home_digest(_, query: CallbackQuery) -> None:
        # A digest is outward-facing and cannot be recalled, so the button asks
        # first — and the confirming button names the action, not just "Yes".
        await query.message.edit_text(
            "▶️ <b>Send digest now?</b>\n\n<i>Everything currently waiting goes out immediately.</i>",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("▶️ Send it", callback_data="home_digest_go"),
                InlineKeyboardButton("« Home", callback_data="home"),
            ]]),
        )

    @bot.on_callback_query(pf.regex(r"^home_digest_go$") & admin_cb)
    async def cb_home_digest_go(_, query: CallbackQuery) -> None:
        await query.answer()
        await _run_digest(query.message)

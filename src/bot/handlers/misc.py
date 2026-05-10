import logging
from html import escape
from pathlib import Path

from pyrogram import filters as pf
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from src.config import settings
from src.db.models import get_categories, get_db
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


def register_misc_handlers(bot, admin_msg, admin_cb) -> None:

    @bot.on_callback_query(pf.regex(r"^noop$") & admin_cb)
    async def cb_noop(_, query: CallbackQuery) -> None:
        await query.answer()

    @bot.on_message(pf.command("digest") & admin_msg)
    async def cmd_digest(_, message: Message) -> None:
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
        async with get_db() as db:
            async with db.execute(
                "SELECT COUNT(*) as total FROM items WHERE processed_at >= datetime('now', '-24 hours')"
            ) as cur:
                total = (await cur.fetchone())["total"]
            async with db.execute("SELECT COUNT(*) as unsent FROM items WHERE sent = 0") as cur:
                unsent = (await cur.fetchone())["unsent"]
            async with db.execute(
                """SELECT sources.name, sources.category, sources.type,
                          COUNT(*) as cnt,
                          SUM(CASE WHEN items.sent = 0 THEN 1 ELSE 0 END) as unsent_cnt
                   FROM items JOIN sources ON items.source_id = sources.id
                   LEFT JOIN categories ON sources.category = categories.name
                   WHERE items.processed_at >= datetime('now', '-24 hours')
                   GROUP BY sources.id
                   ORDER BY COALESCE(categories.sort_order, 999), sources.category,
                            CASE WHEN sources.type = 'telegram' THEN 0 ELSE 1 END,
                            cnt DESC"""
            ) as cur:
                by_source = await cur.fetchall()

        cats = await get_categories()
        cat_emoji = {c["name"]: c["emoji"] for c in cats}

        pending_part = f"  <b>{unsent}</b> pending" if unsent else ""
        lines = ["📊 <b>Stats</b>", "", f"<tg-spoiler><b>{total}</b></tg-spoiler> collected (24h){pending_part}"]

        if by_source:
            by_cat: dict[str, list] = {}
            for r in by_source:
                by_cat.setdefault(r["category"], []).append(r)
            for cat_name, sources in by_cat.items():
                emoji = cat_emoji.get(cat_name, "📌")
                block_lines = [f"{emoji} <b>{escape(cat_name)}</b>"]
                for r in sources:
                    type_label = "tg" if r["type"] == "telegram" else "rss"
                    unsent_part = f"  ({r['unsent_cnt']}⏳)" if r["unsent_cnt"] else ""
                    block_lines.append(f"[{type_label}] {escape(r['name'])} · <tg-spoiler>{r['cnt']}{unsent_part}</tg-spoiler>")
                lines.append("<blockquote expandable>" + "\n".join(block_lines) + "</blockquote>")

        await send_reply(message.chat.id, "\n".join(lines), reply_to_message_id=message.id)

    @bot.on_message(pf.command("logs") & admin_msg)
    async def cmd_logs(_, message: Message) -> None:
        log_file = Path(settings.database_path).parent / "logs" / "sentinel.log"
        if not log_file.exists():
            await message.reply("No log file yet.")
            return
        tail = _tail_lines(log_file, 20)
        text = "<pre>" + escape("\n".join(tail)) + "</pre>"
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

    @bot.on_message(pf.command("start") & admin_msg)
    async def cmd_start(_, message: Message) -> None:
        await message.reply(
            "<b>TelegramSentinel</b>\n\n"
            "/categories — manage categories &amp; sources\n"
            "/blocked — blocked words filter\n"
            "/radar — keyword alerts\n\n"
            "/digest — send digest now\n"
            "/stats — statistics\n"
            "/logs — recent log entries"
        )

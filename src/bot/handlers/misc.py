import logging
from html import escape
from pathlib import Path

import aiosqlite
from pyrogram import filters as pf
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from src.bot.state import _pending
from src.config import settings
from src.db.models import get_categories
from src.dispatcher.digest_builder import send_digest

log = logging.getLogger(__name__)


def register_misc_handlers(bot, admin_msg, admin_cb) -> None:

    @bot.on_message(pf.command("cancel") & admin_msg)
    async def cmd_cancel(_, message: Message) -> None:
        uid = message.from_user.id
        if uid in _pending:
            del _pending[uid]
            await message.reply("Cancelled.")
        else:
            await message.reply("Nothing to cancel.")

    @bot.on_callback_query(pf.regex(r"^cancel_flow$") & admin_cb)
    async def cb_cancel_flow(_, query: CallbackQuery) -> None:
        _pending.pop(query.from_user.id, None)
        await query.message.edit_text("Cancelled.")

    @bot.on_callback_query(pf.regex(r"^noop$") & admin_cb)
    async def cb_noop(_, query: CallbackQuery) -> None:
        await query.answer()

    @bot.on_message(pf.command("digest") & admin_msg)
    async def cmd_digest(_, message: Message) -> None:
        log.info("Manual digest triggered by user")
        status_msg = await message.reply("⏳ Building digest...")
        sent = await send_digest()
        await status_msg.delete()
        await message.reply("✅ Digest sent." if sent else "ℹ️ No new items.")

    @bot.on_message(pf.command("stats") & admin_msg)
    async def cmd_stats(_, message: Message) -> None:
        async with aiosqlite.connect(settings.database_path) as db:
            db.row_factory = aiosqlite.Row
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
                   WHERE items.processed_at >= datetime('now', '-24 hours')
                   GROUP BY sources.id ORDER BY sources.category, cnt DESC"""
            ) as cur:
                by_source = await cur.fetchall()

        cats = await get_categories()
        cat_emoji = {c["name"]: c["emoji"] for c in cats}

        pending_part = f"  <b>{unsent}</b> pending" if unsent else ""
        lines = ["📊 <b>Stats</b>", "", f"<b>{total}</b> collected (24h){pending_part}"]

        if by_source:
            by_cat: dict[str, list] = {}
            for r in by_source:
                by_cat.setdefault(r["category"], []).append(r)
            for cat_name, sources in by_cat.items():
                emoji = cat_emoji.get(cat_name, "📌")
                lines.append(f"\n{emoji} <b>{cat_name}</b>")
                for r in sources:
                    type_label = "tg" if r["type"] == "telegram" else "rss"
                    unsent_part = f"  ({r['unsent_cnt']}⏳)" if r["unsent_cnt"] else ""
                    lines.append(f"  [{type_label}] {escape(r['name'])} · {r['cnt']}{unsent_part}")

        await message.reply("\n".join(lines))

    @bot.on_message(pf.command("logs") & admin_msg)
    async def cmd_logs(_, message: Message) -> None:
        log_file = Path(settings.database_path).parent / "logs" / "sentinel.log"
        if not log_file.exists():
            await message.reply("No log file yet.")
            return
        lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
        tail = lines[-20:] if len(lines) > 20 else lines
        text = "<pre>" + escape("\n".join(tail)) + "</pre>"
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("📥 Download full log", callback_data="logs_download")]])
        await message.reply(text, reply_markup=kb)

    @bot.on_callback_query(pf.regex(r"^logs_download$") & admin_cb)
    async def cb_logs_download(client, query: CallbackQuery) -> None:
        log_file = Path(settings.database_path).parent / "logs" / "sentinel.log"
        if not log_file.exists():
            await query.answer("Log file not found.", show_alert=True)
            return
        await query.answer()
        await client.send_document(query.message.chat.id, str(log_file))

    @bot.on_message(pf.command("start") & admin_msg)
    async def cmd_start(_, message: Message) -> None:
        await message.reply(
            "<b>TelegramSentinel</b>\n\n"
            "/categories — manage categories &amp; sources\n"
            "/blocked — blocked words filter\n\n"
            "/digest — send digest now\n"
            "/stats — statistics\n"
            "/logs — recent log entries\n"
            "/cancel — cancel current input"
        )

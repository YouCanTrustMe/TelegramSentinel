"""Radar entrypoint: wires the keyword/chat/blacklist submodules, the main
menu, the status screen and the single shared add-flow conversation handler."""
import logging
from datetime import datetime, timezone

from pyrogram import filters as pf
from pyrogram.types import CallbackQuery, Message

from src.bot.handlers.radar_blacklist import handle_blacklist_input, register_blacklist
from src.bot.handlers.radar_chats import handle_chat_input, register_chats
from src.bot.handlers.radar_common import _radar_main_kb
from src.bot.handlers.radar_keywords import handle_keyword_input, register_keywords
from src.bot.keyboards import _back_kb
from src.bot.state import _pending
from src.db.models import (
    get_radar_blacklist,
    get_radar_chats,
    get_radar_keywords,
    get_recent_radar_alerts,
)

log = logging.getLogger(__name__)

_start_time = datetime.now(timezone.utc)

_RADAR_INPUT_HANDLERS = {
    "add_radar_keyword": handle_keyword_input,
    "add_radar_chat": handle_chat_input,
    "add_radar_blacklist": handle_blacklist_input,
}


def register_radar_bot_handlers(bot, admin_msg, admin_cb) -> None:
    register_keywords(bot, admin_msg, admin_cb)
    register_chats(bot, admin_msg, admin_cb)
    register_blacklist(bot, admin_msg, admin_cb)

    @bot.on_message(pf.command("radar") & admin_msg)
    async def cmd_radar(_, message: Message) -> None:
        await message.reply(
            "🔍 <b>Radar</b> — real-time keyword alerts",
            reply_markup=_radar_main_kb(),
        )

    @bot.on_callback_query(pf.regex(r"^radar_main$") & admin_cb)
    async def cb_radar_main(_, query: CallbackQuery) -> None:
        _pending.pop(query.from_user.id, None)
        await query.message.edit_text(
            "🔍 <b>Radar</b> — real-time keyword alerts",
            reply_markup=_radar_main_kb(),
        )

    @bot.on_callback_query(pf.regex(r"^radar_status$") & admin_cb)
    async def cb_radar_status(_, query: CallbackQuery) -> None:
        chats = await get_radar_chats()
        keywords = await get_radar_keywords()
        blacklist = await get_radar_blacklist()
        alerts = await get_recent_radar_alerts(3)

        delta = datetime.now(timezone.utc) - _start_time
        hours, rem = divmod(int(delta.total_seconds()), 3600)
        minutes = rem // 60

        alert_lines = ""
        if alerts:
            alert_lines = "\n\n<b>Last alerts:</b>\n" + "\n".join(
                f"• \"{r['keyword']}\" in {r['chat_ref']} — {r['alerted_at'][:16]}"
                for r in alerts
            )

        text = (
            f"📊 <b>Radar Status</b>\n\n"
            f"Chats monitored: <b>{len(chats)}</b>\n"
            f"Keywords active: <b>{len(keywords)}</b>\n"
            f"Blacklisted users: <b>{len(blacklist)}</b>\n"
            f"Uptime: <b>{hours}h {minutes}m</b>"
            f"{alert_lines}"
        )
        await query.message.edit_text(text, reply_markup=_back_kb("radar_main"))

    @bot.on_message(pf.private & admin_msg, group=1)
    async def handle_radar_conversation(_, message: Message) -> None:
        if not message.text or message.text.startswith("/"):
            return
        uid = message.from_user.id
        state = _pending.get(uid)
        if not state or not state.get("action", "").startswith("add_radar"):
            return
        handler = _RADAR_INPUT_HANDLERS.get(state["action"])
        if handler:
            await handler(message, uid, message.text.strip())

"""Radar blacklist: list/add/delete handlers plus the blacklist add-flow text
input. Messages from blacklisted user ids still match keywords, but their alert
is delivered silently (disable_notification)."""
import logging
from html import escape

from pyrogram import filters as pf
from pyrogram.types import CallbackQuery, Message

from src.bot.handlers.radar_common import _radar_list_kb
from src.bot.keyboards import _back_kb, _confirm_keyboard
from src.bot.state import _pending
from src.db.models import add_radar_blacklist, get_radar_blacklist, remove_radar_blacklist

log = logging.getLogger(__name__)


def _blacklist_text(items) -> str:
    return (
        f"🚫 <b>Blacklist</b> ({len(items)})"
        if items
        else "🚫 <b>Blacklist</b>\n\nNo users blacklisted."
    )


def register_blacklist(bot, admin_msg, admin_cb) -> None:

    @bot.on_callback_query(pf.regex(r"^radar_blacklist(:\d+)?$") & admin_cb)
    async def cb_radar_blacklist(_, query: CallbackQuery) -> None:
        _pending.pop(query.from_user.id, None)
        parts = query.data.split(":")
        page = int(parts[1]) if len(parts) > 1 else 0
        items = await get_radar_blacklist()
        kb = _radar_list_kb(
            items, page, "id", "radar_bl_del:", "radar_bl_add",
            "radar_blacklist", lambda r: str(r["user_id"]),
        )
        await query.message.edit_text(_blacklist_text(items), reply_markup=kb)

    @bot.on_callback_query(pf.regex(r"^radar_bl_add$") & admin_cb)
    async def cb_radar_bl_add(_, query: CallbackQuery) -> None:
        uid = query.from_user.id
        _pending[uid] = {"action": "add_radar_blacklist", "step": 0, "data": {}}
        await query.message.edit_text(
            "Send user_id (integer) to blacklist:",
            reply_markup=_back_kb("radar_blacklist:0"),
        )

    @bot.on_callback_query(pf.regex(r"^radar_bl_del:\d+$") & admin_cb)
    async def cb_radar_bl_del(_, query: CallbackQuery) -> None:
        entry_id = int(query.data.split(":")[1])
        items = await get_radar_blacklist()
        row = next((b for b in items if b["id"] == entry_id), None)
        label = str(row["user_id"]) if row else str(entry_id)
        await query.message.edit_text(
            f"Unblacklist user <b>{escape(label)}</b>?",
            reply_markup=_confirm_keyboard(f"radar_bl_del_ok:{entry_id}", "radar_blacklist:0"),
        )

    @bot.on_callback_query(pf.regex(r"^radar_bl_del_ok:\d+$") & admin_cb)
    async def cb_radar_bl_del_ok(_, query: CallbackQuery) -> None:
        entry_id = int(query.data.split(":")[1])
        await remove_radar_blacklist(entry_id)
        log.info("Radar blacklist entry removed: id=%d", entry_id)
        items = await get_radar_blacklist()
        await query.message.edit_text(
            _blacklist_text(items),
            reply_markup=_radar_list_kb(
                items, 0, "id", "radar_bl_del:", "radar_bl_add",
                "radar_blacklist", lambda r: str(r["user_id"]),
            ),
        )


async def handle_blacklist_input(message: Message, uid: int, text: str) -> None:
    if not text.lstrip("-").isdigit():
        await message.reply(
            "Invalid input. Send an integer user_id:",
            reply_markup=_back_kb("radar_blacklist:0"),
        )
        return
    user_id = int(text)
    added = await add_radar_blacklist(user_id)
    del _pending[uid]
    items = await get_radar_blacklist()
    if added:
        log.info("Radar blacklist entry added: user_id=%d", user_id)
        header = f"✅ Blacklisted: <code>{user_id}</code>\n\n🚫 <b>Blacklist</b> ({len(items)})"
    else:
        header = f"⚠️ Already blacklisted: <code>{user_id}</code>"
    await message.reply(
        header,
        reply_markup=_radar_list_kb(
            items, 0, "id", "radar_bl_del:", "radar_bl_add",
            "radar_blacklist", lambda r: str(r["user_id"]),
        ),
    )

"""Radar keywords: list/view/add/delete handlers plus the keyword add-flow
text input."""
import logging
from html import escape

from pyrogram import filters as pf
from pyrogram.types import CallbackQuery, Message

from src.bot.handlers.radar_common import _radar_list_kb, _render_keywords, _chat_label
from src.bot.keyboards import _back_kb, _confirm_keyboard
from src.bot.state import _pending
from src.db.models import (
    add_radar_keyword,
    get_chats_for_keyword,
    get_radar_keywords,
    remove_radar_keyword,
)

log = logging.getLogger(__name__)


def register_keywords(bot, admin_msg, admin_cb) -> None:

    @bot.on_callback_query(pf.regex(r"^radar_keywords(:\d+)?$") & admin_cb)
    async def cb_radar_keywords(_, query: CallbackQuery) -> None:
        _pending.pop(query.from_user.id, None)
        parts = query.data.split(":")
        page = int(parts[1]) if len(parts) > 1 else 0
        text, kb = await _render_keywords(page)
        await query.message.edit_text(text, reply_markup=kb)

    @bot.on_callback_query(pf.regex(r"^radar_kw_view:\d+$") & admin_cb)
    async def cb_radar_kw_view(_, query: CallbackQuery) -> None:
        kw_id = int(query.data.split(":")[1])
        all_kw = await get_radar_keywords()
        kw_row = next((k for k in all_kw if k["id"] == kw_id), None)
        if not kw_row:
            await query.answer("Keyword not found.", show_alert=True)
            return
        linked_chats = await get_chats_for_keyword(kw_id)
        if linked_chats:
            chat_lines = "\n".join(f"• {escape(_chat_label(c))}" for c in linked_chats)
            text = (
                f"📋 <b>{escape(kw_row['keyword'])}</b>\n\n"
                f"Linked to <b>{len(linked_chats)}</b> chat(s):\n{chat_lines}\n\n"
                f"<i>Edit links from the chat side: Chats → tap chat → toggle keywords.</i>"
            )
        else:
            text = (
                f"📋 <b>{escape(kw_row['keyword'])}</b>\n\n"
                f"⚠️ Not linked to any chat yet.\n\n"
                f"<i>Open Chats → tap a chat → toggle this keyword on.</i>"
            )
        await query.message.edit_text(text, reply_markup=_back_kb("radar_keywords:0"))

    @bot.on_callback_query(pf.regex(r"^radar_kw_add$") & admin_cb)
    async def cb_radar_kw_add(_, query: CallbackQuery) -> None:
        uid = query.from_user.id
        _pending[uid] = {"action": "add_radar_keyword", "step": 0, "data": {}}
        await query.message.edit_text(
            "Send keyword to add:",
            reply_markup=_back_kb("radar_keywords:0"),
        )

    @bot.on_callback_query(pf.regex(r"^radar_kw_del:\d+$") & admin_cb)
    async def cb_radar_kw_del(_, query: CallbackQuery) -> None:
        kw_id = int(query.data.split(":")[1])
        items = await get_radar_keywords()
        kw_row = next((k for k in items if k["id"] == kw_id), None)
        label = kw_row["keyword"] if kw_row else str(kw_id)
        await query.message.edit_text(
            f"Remove keyword <b>{escape(label)}</b>?",
            reply_markup=_confirm_keyboard(f"radar_kw_del_ok:{kw_id}", "radar_keywords:0"),
        )

    @bot.on_callback_query(pf.regex(r"^radar_kw_del_ok:\d+$") & admin_cb)
    async def cb_radar_kw_del_ok(_, query: CallbackQuery) -> None:
        kw_id = int(query.data.split(":")[1])
        await remove_radar_keyword(kw_id)
        log.info("Radar keyword removed: id=%d", kw_id)
        text, kb = await _render_keywords(0)
        await query.message.edit_text(text, reply_markup=kb)


async def handle_keyword_input(message: Message, uid: int, text: str) -> None:
    keyword = text.lower()
    added = await add_radar_keyword(keyword)
    del _pending[uid]
    items = await get_radar_keywords()
    if added:
        log.info("Radar keyword added: %s", keyword)
        header = (
            f"✅ Added: <code>{escape(keyword)}</code>\n\n"
            f"📋 <b>Keywords</b> ({len(items)})"
            if items
            else f"✅ Added: <code>{escape(keyword)}</code>\n\n📋 <b>Keywords</b>\n\nNo keywords yet."
        )
    else:
        header = f"⚠️ Already exists: <code>{escape(keyword)}</code>\n\n📋 <b>Keywords</b> ({len(items)})"
    await message.reply(
        header,
        reply_markup=_radar_list_kb(
            items, 0, "id", "radar_kw_del:", "radar_kw_add",
            "radar_keywords", lambda r: r["keyword"],
        ),
    )

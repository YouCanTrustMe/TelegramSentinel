"""Radar monitored chats: list/add/delete, the per-chat keyword-link editor,
and the chat add-flow text input (resolving @usernames, ids and invite links)."""
import logging
from html import escape

from pyrogram import filters as pf
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from src.bot.handlers.radar_common import _PAGE_SIZE, _chat_label, _radar_list_kb, _render_chats
from src.bot.keyboards import _back_kb, _confirm_keyboard
from src.bot.state import _pending
from src.collectors.folder_manager import RADAR_FOLDER, add_to_folder, remove_from_folder
from src.collectors.telegram_collector import userbot
from src.db.models import (
    add_radar_chat,
    get_keyword_ids_for_chat,
    get_radar_chats,
    get_radar_keywords,
    link_keyword_chat,
    remove_radar_chat,
    unlink_keyword_chat,
)

log = logging.getLogger(__name__)


async def _render_chat_edit(chat_id: int, page: int) -> tuple[str, InlineKeyboardMarkup]:
    chats = await get_radar_chats()
    chat_row = next((c for c in chats if c["id"] == chat_id), None)
    if not chat_row:
        return "Chat not found.", _back_kb("radar_chats:0")
    keywords = await get_radar_keywords()
    linked = await get_keyword_ids_for_chat(chat_id)
    total = len(keywords)
    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * _PAGE_SIZE
    page_items = keywords[start:start + _PAGE_SIZE]

    buttons = []
    for k in page_items:
        mark = "✅" if k["id"] in linked else "⬜"
        buttons.append([InlineKeyboardButton(
            f"{mark} {k['keyword']}",
            callback_data=f"radar_link_toggle:{chat_id}:{k['id']}:{page}",
        )])

    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀", callback_data=f"radar_chat_view:{chat_id}:{page - 1}"))
        nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton("▶", callback_data=f"radar_chat_view:{chat_id}:{page + 1}"))
        buttons.append(nav)

    buttons.append([InlineKeyboardButton("◀ Back", callback_data="radar_chats:0")])

    status_line = f" · status: <b>{chat_row['status']}</b>" if chat_row["status"] != "active" else ""
    if keywords:
        text = (
            f"💬 <b>{escape(chat_row['title'] or chat_row['chat_ref'])}</b>"
            f"{status_line}\n\n"
            f"Tap a keyword to toggle monitoring for this chat.\n"
            f"Linked: <b>{len(linked)}</b>/{total}"
        )
    else:
        text = (
            f"💬 <b>{escape(chat_row['title'] or chat_row['chat_ref'])}</b>"
            f"{status_line}\n\n"
            f"⚠️ No keywords defined yet. Add some in Keywords first."
        )
    return text, InlineKeyboardMarkup(buttons)


def register_chats(bot, admin_msg, admin_cb) -> None:

    @bot.on_callback_query(pf.regex(r"^radar_chats(:\d+)?$") & admin_cb)
    async def cb_radar_chats(_, query: CallbackQuery) -> None:
        _pending.pop(query.from_user.id, None)
        parts = query.data.split(":")
        page = int(parts[1]) if len(parts) > 1 else 0
        text, kb = await _render_chats(page)
        await query.message.edit_text(text, reply_markup=kb)

    @bot.on_callback_query(pf.regex(r"^radar_chat_view:\d+(:\d+)?$") & admin_cb)
    async def cb_radar_chat_view(_, query: CallbackQuery) -> None:
        parts = query.data.split(":")
        chat_id = int(parts[1])
        page = int(parts[2]) if len(parts) > 2 else 0
        text, kb = await _render_chat_edit(chat_id, page)
        await query.message.edit_text(text, reply_markup=kb)

    @bot.on_callback_query(pf.regex(r"^radar_link_toggle:\d+:\d+:\d+$") & admin_cb)
    async def cb_radar_link_toggle(_, query: CallbackQuery) -> None:
        _, chat_id_s, kw_id_s, page_s = query.data.split(":")
        chat_id, kw_id, page = int(chat_id_s), int(kw_id_s), int(page_s)
        linked = await get_keyword_ids_for_chat(chat_id)
        if kw_id in linked:
            await unlink_keyword_chat(kw_id, chat_id)
            log.info("Radar link removed: kw_id=%d chat_id=%d", kw_id, chat_id)
        else:
            await link_keyword_chat(kw_id, chat_id)
            log.info("Radar link added: kw_id=%d chat_id=%d", kw_id, chat_id)
        text, kb = await _render_chat_edit(chat_id, page)
        await query.message.edit_text(text, reply_markup=kb)

    @bot.on_callback_query(pf.regex(r"^radar_chat_add$") & admin_cb)
    async def cb_radar_chat_add(_, query: CallbackQuery) -> None:
        uid = query.from_user.id
        _pending[uid] = {"action": "add_radar_chat", "step": 0, "data": {}}
        await query.message.edit_text(
            "Send a chat or channel: @username, chat_id, public t.me link, "
            "or a private +invite link:",
            reply_markup=_back_kb("radar_chats:0"),
        )

    @bot.on_callback_query(pf.regex(r"^radar_chat_del:\d+$") & admin_cb)
    async def cb_radar_chat_del(_, query: CallbackQuery) -> None:
        entry_id = int(query.data.split(":")[1])
        all_chats = await get_radar_chats()
        chat_row = next((c for c in all_chats if c["id"] == entry_id), None)
        label = _chat_label(chat_row) if chat_row else str(entry_id)
        await query.message.edit_text(
            f"Remove monitored chat <b>{escape(label)}</b>?\n"
            f"<i>Userbot will leave the chat.</i>",
            reply_markup=_confirm_keyboard(f"radar_chat_del_ok:{entry_id}", "radar_chats:0"),
        )

    @bot.on_callback_query(pf.regex(r"^radar_chat_del_ok:\d+$") & admin_cb)
    async def cb_radar_chat_del_ok(_, query: CallbackQuery) -> None:
        entry_id = int(query.data.split(":")[1])
        all_chats = await get_radar_chats()
        chat_row = next((c for c in all_chats if c["id"] == entry_id), None)
        await remove_radar_chat(entry_id)
        log.info("Radar chat removed: id=%d", entry_id)
        if chat_row:
            ref = chat_row["chat_ref"]
            await remove_from_folder(ref, RADAR_FOLDER)
            try:
                await userbot.leave_chat(ref if ref.startswith("@") else int(ref))
                log.info("Radar: left chat %s", ref)
            except Exception as exc:
                log.warning("Radar: could not leave chat %s: %s", ref, exc)
        text, kb = await _render_chats(0)
        await query.message.edit_text(text, reply_markup=kb)


async def handle_chat_input(message: Message, uid: int, text: str) -> None:
    raw = text.strip()
    invite = None
    if "t.me/" in raw:
        path = raw.split("t.me/")[1].split("?")[0].rstrip("/")
        if path.startswith("+") or path.startswith("joinchat/"):
            invite = raw
        else:
            raw = f"@{path}"
    elif raw.startswith("+"):
        invite = f"https://t.me/{raw}"

    title = None
    resolved_id: int | None = None
    ref = raw
    if invite:
        try:
            chat = await userbot.join_chat(invite)
            title = chat.title or getattr(chat, "first_name", None) or invite
            resolved_id = chat.id
            ref = str(chat.id)
            log.info("Radar: joined private chat via invite link, id=%s", resolved_id)
        except Exception as exc:
            await message.reply(
                f"Could not join via invite link: <code>{escape(str(exc))}</code>\n"
                "If you are already a member, add it by chat_id instead.",
                reply_markup=_back_kb("radar_chats:0"),
            )
            return
    else:
        if not (raw.startswith("@") or raw.lstrip("-").isdigit()):
            await message.reply(
                "Invalid input. Send @username, chat_id, or a t.me link "
                "(public @name or private +invite):",
                reply_markup=_back_kb("radar_chats:0"),
            )
            return
        try:
            chat = await userbot.get_chat(raw if raw.startswith("@") else int(raw))
            title = chat.title or chat.first_name or raw
            resolved_id = chat.id
            ref = f"@{chat.username}" if chat.username else str(chat.id)
        except Exception as exc:
            ref = raw
            log.warning("Could not resolve radar chat %s: %s", raw, exc)

    added = await add_radar_chat(ref, title, chat_id=resolved_id)
    del _pending[uid]
    if added:
        if not invite:
            try:
                await userbot.join_chat(ref if ref.startswith("@") else int(ref))
                log.info("Radar: joined chat %s", ref)
            except Exception as exc:
                log.warning("Radar: could not join chat %s: %s", ref, exc)
        await add_to_folder(ref, RADAR_FOLDER)
    items = await get_radar_chats()
    if added:
        log.info("Radar chat added: ref=%s title=%s", ref, title)
        header = (
            f"✅ Added: <code>{escape(ref)}</code>"
            + (f" — {escape(title)}" if title else "")
            + f"\n\n💬 <b>Monitored chats</b> ({len(items)})"
        )
    else:
        header = f"⚠️ Already monitored: <code>{escape(ref)}</code>"
    await message.reply(
        header,
        reply_markup=_radar_list_kb(
            items, 0, "id", "radar_chat_del:", "radar_chat_add",
            "radar_chats", _chat_label,
        ),
    )

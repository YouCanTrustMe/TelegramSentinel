import logging
from html import escape

from pyrogram import filters as pf
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from src.bot.keyboards import _back_kb, _blocked_keyboard
from src.bot.state import _pending
from src.db.models import (
    get_blocked_words,
    get_categories,
    get_categories_for_word,
    link_word_category,
    remove_blocked_word,
    unlink_word_category,
)

log = logging.getLogger(__name__)

_BLOCKED_TITLE = "🚫 <b>Content filters</b>"
_BLOCKED_EMPTY = "🚫 <b>Content filters</b>\n\nNo filter rules yet."


async def _render_blocked_view(word_id: int) -> tuple[str, InlineKeyboardMarkup] | None:
    words = await get_blocked_words()
    word = next((w for w in words if w["id"] == word_id), None)
    if not word:
        return None
    categories = await get_categories()
    scoped = set(await get_categories_for_word(word_id))

    buttons, row = [], []
    for cat in categories:
        name = cat["name"]
        mark = "✅" if name in scoped else "⬜"
        # Embed the stable category id (not the name) to keep callback_data short and rename-safe.
        row.append(InlineKeyboardButton(f"{mark} {name}", callback_data=f"blocked_cat_toggle:{word_id}:{cat['id']}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("🗑 Remove", callback_data=f"blocked_del:{word_id}")])
    buttons.append([InlineKeyboardButton("◀ Back", callback_data="blocked_list")])

    applies = f"selected ({len(scoped)})" if scoped else "all categories"
    text = (
        f"🔴 {escape(word['rule'])}\n\n"
        f"<b>Applies to:</b> {applies}\n"
        f"<i>Tap a category to toggle this filter. None selected = applies everywhere.</i>"
    )
    return text, InlineKeyboardMarkup(buttons)


def register_blocked_handlers(bot, admin_msg, admin_cb) -> None:

    @bot.on_message(pf.command("blocked") & admin_msg)
    async def cmd_blocked(_, message: Message) -> None:
        words = await get_blocked_words()
        await message.reply(
            _BLOCKED_TITLE if words else _BLOCKED_EMPTY,
            reply_markup=_blocked_keyboard(words),
        )

    @bot.on_callback_query(pf.regex(r"^blocked_list(:\d+)?$") & admin_cb)
    async def cb_blocked_list(_, query: CallbackQuery) -> None:
        _pending.pop(query.from_user.id, None)
        parts = query.data.split(":")
        page = int(parts[1]) if len(parts) > 1 else 0
        words = await get_blocked_words()
        await query.message.edit_text(
            _BLOCKED_TITLE if words else _BLOCKED_EMPTY,
            reply_markup=_blocked_keyboard(words, page),
        )

    @bot.on_callback_query(pf.regex(r"^blocked_add$") & admin_cb)
    async def cb_blocked_add(_, query: CallbackQuery) -> None:
        uid = query.from_user.id
        _pending[uid] = {"action": "add_blocked_word", "step": 0, "data": {}}
        await query.message.edit_text(
            "Enter a filter rule description (e.g. 'space launches and commercial rockets'):",
            reply_markup=_back_kb("blocked_list"),
        )

    @bot.on_callback_query(pf.regex(r"^blocked_view:") & admin_cb)
    async def cb_blocked_view(_, query: CallbackQuery) -> None:
        word_id = int(query.data.split(":", 1)[1])
        rendered = await _render_blocked_view(word_id)
        if rendered is None:
            await query.answer("Filter rule not found.", show_alert=True)
            return
        text, kb = rendered
        await query.message.edit_text(text, reply_markup=kb)

    @bot.on_callback_query(pf.regex(r"^blocked_cat_toggle:\d+:\d+$") & admin_cb)
    async def cb_blocked_cat_toggle(_, query: CallbackQuery) -> None:
        _, word_id_s, cat_id_s = query.data.split(":")
        word_id, cat_id = int(word_id_s), int(cat_id_s)
        category = next((c["name"] for c in await get_categories() if c["id"] == cat_id), None)
        if category is None:
            await query.answer("Category not found.", show_alert=True)
            return
        if category in set(await get_categories_for_word(word_id)):
            await unlink_word_category(word_id, category)
            log.info("Filter rule id=%d unscoped from category=%s", word_id, category)
        else:
            await link_word_category(word_id, category)
            log.info("Filter rule id=%d scoped to category=%s", word_id, category)
        rendered = await _render_blocked_view(word_id)
        if rendered is None:
            await query.answer("Filter rule not found.", show_alert=True)
            return
        text, kb = rendered
        await query.message.edit_text(text, reply_markup=kb)

    @bot.on_callback_query(pf.regex(r"^blocked_del:") & admin_cb)
    async def cb_blocked_del(_, query: CallbackQuery) -> None:
        word_id = int(query.data.split(":", 1)[1])
        removed = await remove_blocked_word(word_id)
        if removed:
            log.info("Filter rule removed: id=%s", word_id)
        words = await get_blocked_words()
        await query.message.edit_text(
            _BLOCKED_TITLE if words else _BLOCKED_EMPTY,
            reply_markup=_blocked_keyboard(words),
        )

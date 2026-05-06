import logging

from pyrogram import filters as pf
from pyrogram.types import CallbackQuery, Message

from src.bot.keyboards import _back_kb, _blocked_keyboard, _blocked_word_keyboard
from src.bot.state import _pending
from src.db.models import get_blocked_words, remove_blocked_word

log = logging.getLogger(__name__)

_BLOCKED_TITLE = "🚫 <b>Blocked words</b>"
_BLOCKED_EMPTY = "🚫 <b>Blocked words</b>\n\nNo words blocked yet."


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
            "Enter a word or phrase to block:",
            reply_markup=_back_kb("blocked_list"),
        )

    @bot.on_callback_query(pf.regex(r"^blocked_view:") & admin_cb)
    async def cb_blocked_view(_, query: CallbackQuery) -> None:
        word_id = int(query.data.split(":", 1)[1])
        words = await get_blocked_words()
        word = next((w for w in words if w["id"] == word_id), None)
        if not word:
            await query.answer("Word not found.", show_alert=True)
            return
        await query.message.edit_text(
            f"🔴 <b>{word['word']}</b>",
            reply_markup=_blocked_word_keyboard(word_id),
        )

    @bot.on_callback_query(pf.regex(r"^blocked_del:") & admin_cb)
    async def cb_blocked_del(_, query: CallbackQuery) -> None:
        word_id = int(query.data.split(":", 1)[1])
        removed = await remove_blocked_word(word_id)
        if removed:
            log.info("Blocked word removed: id=%s", word_id)
        words = await get_blocked_words()
        await query.message.edit_text(
            _BLOCKED_TITLE if words else _BLOCKED_EMPTY,
            reply_markup=_blocked_keyboard(words),
        )

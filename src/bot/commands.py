import logging

from pyrogram import filters
from pyrogram.errors import MessageNotModified

from src.bot.handlers.blocked import register_blocked_handlers
from src.bot.handlers.categories import register_category_handlers
from src.bot.handlers.conversation import register_conversation_handler
from src.bot.handlers.misc import register_misc_handlers
from src.bot.handlers.sources import register_source_handlers
from src.bot.handlers.timetable import register_timetable_handlers
from src.config import settings
from src.dispatcher.sender import bot


class _DropUnchangedEdits(logging.Filter):
    """A second tap on a button that redraws the same screen (« pressed twice) makes Telegram
    refuse the edit as MESSAGE_NOT_MODIFIED; the screen is already right, yet the
    dispatcher logged it as an ERROR with a 30-line traceback (2026-10-20)."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not (record.exc_info and isinstance(record.exc_info[1], MessageNotModified))


_DROP_UNCHANGED_EDITS = _DropUnchangedEdits()


def register_commands() -> None:
    logging.getLogger("pyrogram.dispatcher").addFilter(_DROP_UNCHANGED_EDITS)
    admin_msg = filters.user(settings.telegram_admin_id) & filters.private
    admin_cb = filters.user(settings.telegram_admin_id)
    register_category_handlers(bot, admin_msg, admin_cb)
    register_source_handlers(bot, admin_msg, admin_cb)
    register_blocked_handlers(bot, admin_msg, admin_cb)
    register_timetable_handlers(bot, admin_msg, admin_cb)
    register_misc_handlers(bot, admin_msg, admin_cb)
    register_conversation_handler(bot, admin_msg, admin_cb)

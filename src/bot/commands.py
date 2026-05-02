from pyrogram import filters

from src.bot.handlers.blocked import register_blocked_handlers
from src.bot.handlers.categories import register_category_handlers
from src.bot.handlers.conversation import register_conversation_handler
from src.bot.handlers.misc import register_misc_handlers
from src.bot.handlers.sources import register_source_handlers
from src.config import settings
from src.dispatcher.sender import bot


def register_commands() -> None:
    admin_msg = filters.user(settings.telegram_admin_id) & filters.private
    admin_cb = filters.user(settings.telegram_admin_id)
    register_category_handlers(bot, admin_msg, admin_cb)
    register_source_handlers(bot, admin_msg, admin_cb)
    register_blocked_handlers(bot, admin_msg, admin_cb)
    register_misc_handlers(bot, admin_msg, admin_cb)
    register_conversation_handler(bot, admin_msg, admin_cb)

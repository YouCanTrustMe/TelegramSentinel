import logging

from pyrogram import Client
from pyrogram.enums import ParseMode

from src.config import settings

log = logging.getLogger(__name__)

bot = Client(
    "sessions/sentinel_bot",
    bot_token=settings.telegram_bot_token,
    api_id=settings.telegram_api_id,
    api_hash=settings.telegram_api_hash,
)


async def send_message(text: str) -> None:
    await bot.send_message(
        chat_id=settings.telegram_supergroup_id,
        text=text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )
    log.debug("Message sent to supergroup (%d chars)", len(text))

import logging

import aiohttp
from pyrogram import Client

from src.config import settings

log = logging.getLogger(__name__)

bot = Client(
    "sessions/sentinel_bot",
    bot_token=settings.telegram_bot_token,
    api_id=settings.telegram_api_id,
    api_hash=settings.telegram_api_hash,
)

_BOT_API = f"https://api.telegram.org/bot{settings.telegram_bot_token}"


async def send_message(text: str) -> None:
    url = f"{_BOT_API}/sendMessage"
    payload = {
        "chat_id": settings.telegram_supergroup_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload) as resp:
            if resp.status != 200:
                body = await resp.text()
                log.error("Bot API sendMessage failed: %s %s", resp.status, body)
                raise RuntimeError(f"sendMessage failed: {resp.status}")
    log.debug("Message sent to supergroup (%d chars)", len(text))

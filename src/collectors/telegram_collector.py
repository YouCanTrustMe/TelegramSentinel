import asyncio
import logging
from datetime import datetime, timezone

from pyrogram import Client
from pyrogram.types import Message

from src.config import settings
from src.db.models import get_active_sources, save_item
from src.processor.classifier import classify
from src.processor.deduplicator import is_duplicate, make_message_id

log = logging.getLogger(__name__)

POLL_INTERVAL = 300  # seconds between channel polls

userbot = Client(
    "sessions/sentinel_userbot",
    api_id=settings.telegram_api_id,
    api_hash=settings.telegram_api_hash,
    phone_number=settings.telegram_phone,
)


async def _process_message(username: str, source: dict, message: Message) -> bool:
    caption = message.text or message.caption or ""
    if message.photo:
        media_prefix = "[Photo] "
    elif message.video:
        media_prefix = "[Video] "
    elif message.animation:
        media_prefix = "[GIF] "
    else:
        media_prefix = ""

    raw_text = (media_prefix + caption).strip()
    if not raw_text:
        return False

    message_id = make_message_id("telegram", f"@{username}", str(message.id))
    if await is_duplicate(message_id):
        return False

    original_url = f"https://t.me/{username}/{message.id}"
    published_at = message.date.replace(tzinfo=timezone.utc).isoformat() if message.date else None

    if raw_text in ("[Photo]", "[Video]", "[GIF]"):
        summary = raw_text
    else:
        result = await classify(raw_text)
        summary = result.summary

    await save_item(
        source_id=source["id"],
        message_id=message_id,
        raw_text=raw_text,
        original_url=original_url,
        published_at=published_at,
        summary=summary,
        category=source["category"],
        processed_at=datetime.now(timezone.utc).isoformat(),
    )
    log.info("Saved item from @%s | category=%s | %s", username, source["category"], original_url)
    return True


async def _poll_channel(username: str, source: dict) -> int:
    saved = 0
    try:
        async for message in userbot.get_chat_history(f"@{username}", limit=20):
            if await _process_message(username, source, message):
                saved += 1
    except Exception as exc:
        log.error("Failed to poll @%s: %s", username, exc)
    return saved


async def run_telegram_collector() -> None:
    log.info("Telegram collector started (interval=%ds)", POLL_INTERVAL)
    while True:
        try:
            sources = await get_active_sources(type_="telegram")
            for row in sources:
                username = row["url"].lstrip("@").lower()
                source = {"id": row["id"], "name": row["name"], "category": row["category"]}
                saved = await _poll_channel(username, source)
                if saved:
                    log.info("Telegram poll @%s: %d new items", username, saved)
                else:
                    log.debug("Telegram poll @%s: 0 new items", username)
        except Exception as exc:
            log.exception("Telegram collector iteration failed: %s", exc)
        await asyncio.sleep(POLL_INTERVAL)

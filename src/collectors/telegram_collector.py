import logging
from datetime import datetime, timezone

from pyrogram import Client, filters
from pyrogram.types import Message

from src.config import settings
from src.db.models import get_active_sources, save_item
from src.processor.classifier import classify
from src.processor.deduplicator import is_duplicate, make_message_id

log = logging.getLogger(__name__)

userbot = Client(
    "sessions/sentinel_userbot",
    api_id=settings.telegram_api_id,
    api_hash=settings.telegram_api_hash,
    phone_number=settings.telegram_phone,
)

_watched: dict[str, dict] = {}


async def load_watched_channels() -> None:
    sources = await get_active_sources(type_="telegram")
    _watched.clear()
    for row in sources:
        username = row["url"].lstrip("@").lower()
        _watched[username] = {"id": row["id"], "name": row["name"], "category": row["category"]}
    log.info("Watching %d Telegram channel(s): %s", len(_watched), list(_watched.keys()))


async def _process_message(username: str, source: dict, message: Message) -> bool:
    """Process a single channel message: dedup, classify, save. Returns True if saved."""
    raw_text = message.text or message.caption or ""
    if not raw_text.strip():
        return False

    message_id = make_message_id("telegram", f"@{username}", str(message.id))
    if await is_duplicate(message_id):
        log.debug("Skipping duplicate: %s", message_id)
        return False

    original_url = f"https://t.me/{username}/{message.id}"
    published_at = message.date.replace(tzinfo=timezone.utc).isoformat() if message.date else None

    result = await classify(raw_text)

    await save_item(
        source_id=source["id"],
        message_id=message_id,
        raw_text=raw_text,
        original_url=original_url,
        published_at=published_at,
        summary=result.summary,
        category=source["category"],
        importance=result.importance,
        processed_at=datetime.now(timezone.utc).isoformat(),
    )
    log.info(
        "Saved item from @%s | category=%s importance=%s | %s",
        username, source["category"], result.importance, original_url,
    )
    return True


async def backfill_channels(limit: int = 50) -> None:
    """Fetch recent history from all watched channels to recover messages missed during downtime."""
    if not _watched:
        return
    log.info("Backfilling last %d messages from %d channel(s)", limit, len(_watched))
    for username, source in _watched.items():
        saved = 0
        try:
            async for message in userbot.get_chat_history(username, limit=limit):
                if await _process_message(username, source, message):
                    saved += 1
        except Exception as exc:
            log.warning("Backfill failed for @%s: %s", username, exc)
        log.info("Backfill @%s: %d new item(s) saved", username, saved)


def register_handlers() -> None:
    @userbot.on_message(filters.channel)
    async def on_channel_message(client: Client, message: Message) -> None:
        username = (message.chat.username or "").lower()
        source = _watched.get(username)
        if not source:
            return
        try:
            await _process_message(username, source, message)
        except Exception as exc:
            log.error("Error processing message from @%s msg_id=%s: %s", username, message.id, exc)

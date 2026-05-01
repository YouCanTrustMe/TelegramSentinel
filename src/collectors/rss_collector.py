import asyncio
import logging
from datetime import datetime, timezone

import feedparser

from src.db.models import get_active_sources, save_item
from src.processor.classifier import classify
from src.processor.deduplicator import is_duplicate, make_message_id

log = logging.getLogger(__name__)

POLL_INTERVAL = 900


async def fetch_feed(source_id: int, name: str, url: str, category: str) -> int:
    log.info("Polling RSS source '%s' (%s)", name, url)
    feed = await asyncio.to_thread(feedparser.parse, url)
    saved = 0

    for entry in feed.entries:
        entry_url = entry.get("link", "")
        message_id = make_message_id("rss", url, entry_url or entry.get("id", url))

        if await is_duplicate(message_id):
            log.debug("Skipping duplicate: %s", message_id)
            continue

        raw_text = entry.get("summary") or entry.get("title", "")
        if not raw_text.strip():
            continue

        published_at = None
        if hasattr(entry, "published_parsed") and entry.published_parsed:
            published_at = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc).isoformat()

        result = await classify(raw_text)

        await save_item(
            source_id=source_id,
            message_id=message_id,
            raw_text=raw_text,
            original_url=entry_url or None,
            published_at=published_at,
            summary=result.summary,
            category=category,
            importance=result.importance,
            processed_at=datetime.now(timezone.utc).isoformat(),
        )
        log.info(
            "Saved item from '%s' | category=%s importance=%s | %s",
            name, category, result.importance, (entry_url or message_id)[:80],
        )
        saved += 1

    log.info("RSS '%s': %d new items", name, saved)
    return saved


async def run_rss_collector() -> None:
    log.info("RSS collector started (interval=%ds)", POLL_INTERVAL)
    while True:
        sources = await get_active_sources(type_="rss")
        tasks = [fetch_feed(r["id"], r["name"], r["url"], r["category"]) for r in sources]
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for source, result in zip(sources, results):
                if isinstance(result, Exception):
                    log.error("RSS feed '%s' failed: %s", source["name"], result)
        await asyncio.sleep(POLL_INTERVAL)

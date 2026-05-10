import asyncio
import html
import logging
import re
from datetime import datetime, timezone

import feedparser

from src.config import settings
from src.db.models import get_active_sources, save_item, update_source_status
from src.dispatcher.sender import send_to
from src.processor.classifier import classify
from src.processor.deduplicator import is_duplicate, make_message_id

log = logging.getLogger(__name__)


def _strip_html(text: str) -> str:
    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()

POLL_INTERVAL = 900


_PERMANENT_HTTP_ERRORS = {404, 410}


async def fetch_feed(source_id: int, name: str, url: str, category: str, prompt_extra: str | None = None) -> int:
    log.info("Polling RSS source '%s' (%s)", name, url)
    feed = await asyncio.to_thread(feedparser.parse, url)
    saved = 0

    http_status = getattr(feed, "status", None)
    if http_status in _PERMANENT_HTTP_ERRORS:
        log.error("RSS source '%s' returned HTTP %d | marking error", name, http_status)
        await update_source_status(source_id, "error")
        await send_to(
            settings.telegram_admin_id,
            f"⚠️ <b>Source error</b>\n"
            f"<b>{name}</b> returned HTTP {http_status}.\n"
            f"Status set to <b>error</b>.\n"
            f"<i>{url}</i>",
        )
        return 0

    for entry in feed.entries:
        entry_url = entry.get("link", "")
        message_id = make_message_id("rss", url, entry_url or entry.get("id", url))

        if await is_duplicate(message_id):
            log.debug("Skipping duplicate: %s", message_id)
            continue

        title = entry.get("title", "")
        summary_html = entry.get("summary", "")
        raw_text = _strip_html(summary_html) or _strip_html(title)
        if not raw_text:
            continue

        published_at = None
        if hasattr(entry, "published_parsed") and entry.published_parsed:
            published_at = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc).isoformat()

        result = await classify(raw_text, prompt_extra=prompt_extra)

        await save_item(
            source_id=source_id,
            message_id=message_id,
            raw_text=raw_text,
            original_url=entry_url or None,
            published_at=published_at,
            summary=result.summary,
            category=category,
            processed_at=datetime.now(timezone.utc).isoformat(),
            key_phrase=result.key_phrase,
        )
        log.info("Saved item from '%s' | category=%s | %s", name, category, (entry_url or message_id)[:80])
        saved += 1

    log.info("RSS '%s': %d new items", name, saved)
    return saved


async def run_rss_collector() -> None:
    log.info("RSS collector started (interval=%ds)", POLL_INTERVAL)
    while True:
        try:
            sources = await get_active_sources(type_="rss")
            tasks = [fetch_feed(r["id"], r["name"], r["url"], r["category"], r["prompt_extra"]) for r in sources]
            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for source, result in zip(sources, results):
                    if isinstance(result, Exception):
                        log.error("RSS feed '%s' failed: %s", source["name"], result)
        except Exception as exc:
            log.exception("RSS collector iteration failed: %s", exc)
        await asyncio.sleep(POLL_INTERVAL)

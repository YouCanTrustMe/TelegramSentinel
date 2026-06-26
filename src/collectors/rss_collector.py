import asyncio
import html
import logging
import re
from datetime import datetime, timezone

import feedparser

from src.config import settings
from src.db.models import (
    get_active_sources,
    get_db,
    increment_source_fail_count,
    reset_source_fail_count,
    save_item,
    update_source_status,
)
from src.dispatcher.sender import send_to
from src.processor.deduplicator import is_duplicate, make_message_id

log = logging.getLogger(__name__)


def _strip_html(text: str) -> str:
    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _compose_raw_text(title: str, body: str) -> str:
    """Combine a feed entry's headline (title) and description/standfirst (body).
    The headline is the most informative part; some feeds (e.g. FT) put the real
    news in the title and only a vague standfirst in the description, so keeping
    the description alone lost the story. Combine when they are distinct; otherwise
    use whichever is non-empty (avoid duplicating one inside the other)."""
    title = (title or "").strip()
    body = (body or "").strip()
    if not title:
        return body
    if not body:
        return title
    if title.lower() in body.lower():
        return body  # body already contains the headline → it is the superset
    if body.lower() in title.lower():
        return title  # title already contains the body → it is the superset
    return f"{title}. {body}"

POLL_INTERVAL = 900
_BOOTSTRAP_LIMIT = 10

# Some feeds (Cloudflare-fronted, e.g. CoinTelegraph) reject feedparser's default UA with 403/404.
_FEED_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

# A single bad response is usually transient; only disable a feed after this many consecutive failures.
_FAIL_THRESHOLD = 3


async def _mark_failure(source_id: int, name: str, url: str, reason: str) -> None:
    """Count a consecutive failure; disable the source once it crosses the threshold.

    fail_count is NOT reset here: it keeps climbing so the daily revive job can tell
    a transient hiccup (recovers on revive, reset to 0 by a successful poll) from a
    genuinely dead feed (keeps re-failing) and eventually stop reviving it. The admin
    alert fires only on the first crossing so a dead feed does not spam daily."""
    fails = await increment_source_fail_count(source_id)
    # A lone first failure is almost always a transient remote hiccup (502/500,
    # momentary unreachability) that the next poll clears, so keep it at DEBUG;
    # only escalate to WARNING once it repeats and looks like a real outage.
    level = logging.WARNING if fails >= 2 else logging.DEBUG
    log.log(level, "RSS source '%s' failed (%d/%d): %s", name, fails, _FAIL_THRESHOLD, reason)
    if fails >= _FAIL_THRESHOLD:
        await update_source_status(source_id, "error")
        if fails == _FAIL_THRESHOLD:
            await send_to(
                settings.telegram_admin_id,
                f"⚠️ <b>Source error</b>\n"
                f"<b>{name}</b> failed {fails} times ({reason}).\n"
                f"Status set to <b>error</b>.\n"
                f"<i>{url}</i>",
            )


async def fetch_feed(source_id: int, name: str, url: str, category: str, prompt_extra: str | None = None) -> int:
    log.info("Polling RSS source '%s' (%s)", name, url)
    try:
        feed = await asyncio.to_thread(feedparser.parse, url, agent=_FEED_AGENT)
    except Exception as exc:
        await _mark_failure(source_id, name, url, f"parse error: {exc}")
        return 0
    saved = 0

    http_status = getattr(feed, "status", None)
    http_failed = isinstance(http_status, int) and http_status >= 400
    # bozo + zero entries only counts as a failure when we did NOT get a clean 200:
    # a 200 with a benign parse warning and no items is just an empty feed, not a broken one.
    unreachable = not feed.entries and getattr(feed, "bozo", False) and http_status != 200
    if http_failed or unreachable:
        reason = f"HTTP {http_status}" if http_failed else "unreachable / no parseable entries"
        await _mark_failure(source_id, name, url, reason)
        return 0

    await reset_source_fail_count(source_id)

    async with get_db() as db:
        async with db.execute("SELECT COUNT(*) FROM items WHERE source_id = ?", (source_id,)) as cur:
            row = await cur.fetchone()
            is_new = row[0] == 0

    entries = feed.entries[:_BOOTSTRAP_LIMIT] if is_new else feed.entries
    log.info("RSS '%s': %d feed entries (bootstrap=%s)", name, len(entries), is_new)

    for entry in entries:
        entry_url = entry.get("link", "")
        message_id = make_message_id("rss", url, entry_url or entry.get("id", url))

        if await is_duplicate(message_id):
            log.debug("Skipping duplicate: %s", message_id)
            continue

        raw_text = _compose_raw_text(_strip_html(entry.get("title", "")), _strip_html(entry.get("summary", "")))
        if not raw_text:
            continue

        published_at = None
        if hasattr(entry, "published_parsed") and entry.published_parsed:
            published_at = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc).isoformat()

        if len(raw_text.strip()) < 15:
            summary = raw_text.strip()
            key_phrase = ""
        else:
            summary = ""
            key_phrase = ""

        await save_item(
            source_id=source_id,
            message_id=message_id,
            raw_text=raw_text,
            original_url=entry_url or None,
            published_at=published_at,
            summary=summary,
            category=category,
            processed_at=datetime.now(timezone.utc).isoformat(),
            key_phrase=key_phrase,
        )
        log.info("Saved item from '%s' | category=%s | %s", name, category, (entry_url or message_id)[:80])
        saved += 1

    log.info("RSS '%s': %d new items", name, saved)
    return saved


async def poll_rss_once() -> None:
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


async def run_rss_collector() -> None:
    log.info("RSS collector started (interval=%ds)", POLL_INTERVAL)
    while True:
        await poll_rss_once()
        await asyncio.sleep(POLL_INTERVAL)

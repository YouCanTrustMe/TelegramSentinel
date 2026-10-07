import asyncio
import html
import logging
import re
from datetime import datetime, timedelta, timezone

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
from src.processor.dedup.deduplicator import is_duplicate, make_message_id

log = logging.getLogger(__name__)


def _strip_html(text: str) -> str:
    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# WordPress appends "The post <title> first appeared on <site>." to every feed body
# (Croatia Week and 01portal: 100 of 100 entries). It repeats the headline, so it also
# fooled the containment check below into dropping the real title.
# Matched on the entry's own title, so a body that merely says "the post office first
# appeared on stamps" keeps its last sentence.
_WORDPRESS_FOOTER = r"\s*The post {title} (?:first appeared on|appeared first on|prvi put objavljen na) [^\n]{{1,80}}$"


def _strip_wordpress_footer(title: str, body: str) -> str:
    if not title:
        return body
    return re.sub(_WORDPRESS_FOOTER.format(title=re.escape(title)), "", body)


def _compose_raw_text(title: str, body: str) -> str:
    """Combine a feed entry's headline (title) and description/standfirst (body).
    The headline is the most informative part; some feeds (e.g. FT) put the real
    news in the title and only a vague standfirst in the description, so keeping
    the description alone lost the story. Combine when they are distinct; otherwise
    use whichever is non-empty (avoid duplicating one inside the other)."""
    title = (title or "").strip()
    body = _strip_wordpress_footer(title, (body or "").strip())
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
# ...and other Cloudflare setups do the opposite: a browser UA from a server IP has no
# matching browser fingerprint, so it reads as a bot in disguise and gets 403 (jack-clark.net),
# while an honest crawler UA passes. Neither UA works everywhere, so a 403/404 is retried once
# with the other one before it counts as a failure.
_FEED_AGENT_FALLBACK = "feedparser/6.0.11 +https://github.com/kurtmckee/feedparser/"
_UA_BLOCK_STATUSES = (403, 404)


def _agent_blocked(feed: object) -> bool:
    """True when the response looks like a UA-based block rather than a real outage."""
    return getattr(feed, "status", None) in _UA_BLOCK_STATUSES


async def _parse_with_ua_fallback(url: str, name: str):
    """Parse a feed with the browser UA, retrying once with the crawler UA on a
    UA-shaped block. Returns the better of the two responses."""
    feed = await asyncio.to_thread(feedparser.parse, url, agent=_FEED_AGENT)
    if not _agent_blocked(feed):
        return feed
    retry = await asyncio.to_thread(feedparser.parse, url, agent=_FEED_AGENT_FALLBACK)
    if _agent_blocked(retry):
        return feed  # blocked either way → a real block, report the original status
    log.info("RSS '%s': HTTP %s on the browser UA, served on the crawler UA",
             name, getattr(feed, "status", None))
    return retry

# A single bad response is usually transient; only disable a feed after this many consecutive failures.
_FAIL_THRESHOLD = 3
# A disabled feed is re-probed every 30 minutes (scheduler `revive_rss_fast`), one more
# failure each, so 8 in a row is a feed that has been down for about three hours. Its own
# outages are shorter: hnrss.org's 502 on 2026-10-06 cleared within the hour, and still
# sent the admin two messages at 03:15.
_REMOTE_ALERT_FAILS = 8
# A 4xx is OUR problem — a moved, renamed or newly gated feed — and will not heal.
_CLIENT_ALERT_FAILS = 2


def _is_client_error(status: int) -> bool:
    """A 4xx that a human must fix (moved, renamed, gated feed). 408 and 429 are the
    server asking us to wait — they heal by themselves like a 5xx."""
    return 400 <= status < 500 and status not in (408, 429)


def _down_alert(name: str, url: str, reason: str, fails: int, remote_fault: bool) -> str:
    # The reason can be an exception text such as "<urlopen error ...>": unescaped, it
    # makes the HTML message unparseable and the only alert for the outage is lost.
    name, url, reason = html.escape(name), html.escape(url), html.escape(reason)
    if remote_fault:
        return (f"⚠️ <b>Source down</b>\n<b>{name}</b> has failed {fails} polls in a row (~3h, {reason}).\n"
                f"It is re-probed every 30 min and will resume by itself.\n<i>{url}</i>")
    return (f"⚠️ <b>Source broken</b>\n<b>{name}</b> answered {reason} — the feed moved or is "
            f"gated, check the URL.\n<i>{url}</i>")


async def _mark_failure(source_id: int, name: str, url: str, reason: str,
                        remote_fault: bool = True) -> None:
    """Count a consecutive failure; disable the source once it crosses the threshold.

    fail_count is NOT reset here: it keeps climbing through the re-probes, so it doubles
    as "how long has this been down". A successful poll resets it to 0. The admin hears
    about it through one channel (this message — the log line stays INFO, or the WARNING
    forwarder would send it twice) and only when waiting has stopped helping: once for an
    outage, and for a 4xx early plus once more if it is still broken ~3h later."""
    fails = await increment_source_fail_count(source_id)
    log.log(logging.INFO if fails >= _FAIL_THRESHOLD else logging.DEBUG,
            "RSS source '%s' failed (%d in a row): %s", name, fails, reason)
    if fails >= _FAIL_THRESHOLD:
        await update_source_status(source_id, "error")
    # A 4xx alerts early; ANY failure at the long mark alerts, so a feed whose errors
    # switch between 5xx and 4xx cannot step past both thresholds and never be reported.
    if fails == _REMOTE_ALERT_FAILS or (not remote_fault and fails == _CLIENT_ALERT_FAILS):
        log.info("RSS source '%s' still failing after %d polls, alerting the admin", name, fails)
        await send_to(settings.telegram_admin_id, _down_alert(name, url, reason, fails, remote_fault))


async def fetch_feed(source_id: int, name: str, url: str, category: str, prompt_extra: str | None = None) -> int:
    log.info("Polling RSS source '%s' (%s)", name, url)
    try:
        feed = await _parse_with_ua_fallback(url, name)
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
        await _mark_failure(source_id, name, url, reason,
                            remote_fault=not (http_failed and _is_client_error(http_status)))
        return 0

    await reset_source_fail_count(source_id)

    async with get_db() as db:
        async with db.execute("SELECT COUNT(*) FROM items WHERE source_id = ?", (source_id,)) as cur:
            row = await cur.fetchone()
            is_new = row[0] == 0

    entries = feed.entries
    log.info("RSS '%s': %d feed entries (bootstrap=%s)", name, len(entries), is_new)
    now = datetime.now(timezone.utc)
    max_age = timedelta(hours=settings.max_item_age_hours)
    overflow = old = fresh = 0

    for entry in entries:
        entry_url = entry.get("link", "")
        message_id = make_message_id("rss", url, entry_url or entry.get("id", url))

        if await is_duplicate(message_id):
            log.debug("Skipping duplicate: %s", message_id)
            continue

        raw_text = _compose_raw_text(_strip_html(entry.get("title", "")), _strip_html(entry.get("summary", "")))
        if not raw_text:
            continue

        published = _entry_time(entry)
        published_at = published.isoformat() if published else None

        # Stored and retired, not skipped: the stored message_id is the feed's only
        # memory, so a merely skipped entry would come back on the next poll. Age goes
        # first, so a feed listing oldest first still shows its fresh entries on day one.
        # Older entries are recorded as seen and never shown; a feed can also re-surface old ones.
        if published and now - published > max_age:
            old += 1
            await _save_retired(source_id, message_id, raw_text, entry_url, published_at, category)
            continue
        if is_new and fresh >= _BOOTSTRAP_LIMIT:
            overflow += 1
            await _save_retired(source_id, message_id, raw_text, entry_url, published_at, category)
            continue
        fresh += 1

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

    if overflow or old:
        log.info("RSS '%s': %d new items, retired unshown: %d past the first %d of a new source, %d older than %dh",
                 name, saved, overflow, _BOOTSTRAP_LIMIT, old, settings.max_item_age_hours)
    else:
        log.info("RSS '%s': %d new items", name, saved)
    return saved


def _entry_time(entry) -> datetime | None:
    # An Atom entry may carry only <updated>, which feedparser does not copy into published.
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    return datetime(*parsed[:6], tzinfo=timezone.utc) if parsed else None


async def _save_retired(source_id: int, message_id: str, raw_text: str, entry_url: str,
                        published_at: str | None, category: str) -> None:
    # A non-empty summary, or the classifier's backfill would pick the row up, embed it and
    # put an item nobody saw into the dedup comparison pool (see discard_unsent_items).
    # processed_at stays "now", not the entry's date: retention prunes by it, and a month-old
    # entry pruned at night would be stored again by the next poll, every day.
    await save_item(
        source_id=source_id,
        message_id=message_id,
        raw_text=raw_text,
        original_url=entry_url or None,
        published_at=published_at,
        summary=raw_text[:200],
        category=category,
        processed_at=datetime.now(timezone.utc).isoformat(),
        key_phrase="",
        sent=True,
    )


async def skip_feed_to_head(source_id: int, name: str, url: str, category: str) -> int | None:
    """Record everything the feed currently offers as seen-and-retired, so a resumed
    source starts from now instead of replaying the whole pause.

    An RSS feed has no cursor — the collector's only memory is the message_id of each
    entry it has stored — so "skip what happened while paused" has to mean storing those
    entries and retiring them, the same thing pausing does to the queue it inherits."""
    try:
        feed = await _parse_with_ua_fallback(url, name)
    except Exception as exc:
        # None, not 0: "the feed did not answer" and "the feed had nothing new" lead to
        # opposite promises on the resume screen.
        log.warning("Could not read '%s' to skip its backlog on resume: %s", name, exc)
        return None
    saved = 0
    for entry in feed.entries:
        entry_url = entry.get("link", "")
        message_id = make_message_id("rss", url, entry_url or entry.get("id", url))
        if await is_duplicate(message_id):
            continue
        raw_text = _compose_raw_text(_strip_html(entry.get("title", "")), _strip_html(entry.get("summary", "")))
        if not raw_text:
            continue
        published = _entry_time(entry)
        await _save_retired(source_id, message_id, raw_text, entry_url,
                            published.isoformat() if published else None, category)
        saved += 1
    log.info("RSS '%s': marked %d entry/entries as seen without showing them", name, saved)
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

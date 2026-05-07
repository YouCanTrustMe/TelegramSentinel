import logging
import re
from collections import defaultdict
from datetime import datetime, timezone
from html import escape
from zoneinfo import ZoneInfo

from src.config import settings
from src.db.models import get_app_setting, get_blocked_words, get_categories, get_unsent_items, log_digest, mark_sent, set_app_setting
from src.dispatcher.sender import pin_message, send_message, unpin_message
from src.processor.classifier import group_by_topic

log = logging.getLogger(__name__)

_TELEGRAM_LIMIT = 4000
_MAX_ITEMS_PER_SOURCE = 50
_STARS_RE = re.compile(r'^([★☆]{5})\s*')


def _get_tz() -> ZoneInfo:
    return ZoneInfo(settings.digest_timezone)


def _format_item(item: dict) -> str:
    url = item["original_url"] or ""
    summary_text = item["summary"] or ""
    if not summary_text:
        raw = item["raw_text"] or ""
        summary_text = raw[:60].split("\n")[0]

    stars = ""
    m = _STARS_RE.match(summary_text)
    if m:
        stars = m.group(1)
        summary_text = summary_text[m.end():]

    summary = escape(summary_text)
    hour = ""
    pub = item["published_at"]
    if pub:
        try:
            dt = datetime.fromisoformat(pub)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            dt_local = dt.astimezone(_get_tz())
            hour = f"{dt_local.hour}⏰"
        except Exception:
            pass

    parts = [p for p in [hour, stars] if p]
    prefix = " · ".join(parts) + " · " if parts else ""
    if url:
        return f'{prefix}{summary} <a href="{escape(url, quote=True)}">🔗</a>'
    return f"{prefix}{summary}"


async def _merge_source_items(items: list) -> list[dict]:
    if len(items) <= 1:
        return [
            {
                "summary": item["summary"],
                "original_url": item["original_url"],
                "published_at": item["published_at"],
                "raw_text": item["raw_text"],
            }
            for item in items
        ]

    raw_inputs = [{"id": i, "text": item["raw_text"] or ""} for i, item in enumerate(items)]
    try:
        groups = await group_by_topic(raw_inputs)
        merged = []
        for g in groups:
            group_items = [items[i] for i in g["ids"]]
            url = next((x["original_url"] for x in group_items if x["original_url"]), None)
            pub = max(
                (x["published_at"] for x in group_items if x["published_at"]),
                default=None,
            )
            summary = g["summary"] or group_items[0]["summary"] or ""
            n = len(g["ids"])
            if n > 1:
                summary = f"{summary} · merged {n}"
            merged.append({
                "summary": summary,
                "original_url": url,
                "published_at": pub,
                "raw_text": None,
            })
        return merged
    except Exception as exc:
        log.warning("Topic merging failed, using original items: %s", exc)
        return [
            {
                "summary": item["summary"],
                "original_url": item["original_url"],
                "published_at": item["published_at"],
                "raw_text": item["raw_text"],
            }
            for item in items
        ]


def _source_block(source_name: str, source_items: list) -> str:
    block_lines = [f"<b>{escape(source_name)}</b>"]
    for item in source_items:
        block_lines.append(_format_item(item))
    return "<blockquote expandable>" + "\n".join(block_lines) + "</blockquote>"


def _build_digest_text(cat_meta: dict, date_str: str, blocked_items: list | None = None) -> list[str]:
    lines = [f"<b>📋 Digest — {date_str}</b>"]

    for cat_name, data in cat_meta.items():
        sources = data["sources"]
        if not any(sources.values()):
            continue

        lines.append(f"\n<b>{data['emoji']} {cat_name.capitalize()}</b>")

        for source_name, source_items in sources.items():
            if not source_items:
                continue
            lines.append(_source_block(source_name, source_items))

    if blocked_items:
        lines.append("\n<b>🚫 Filtered</b>")
        filtered_by_source: dict[str, list] = defaultdict(list)
        for item in blocked_items:
            filtered_by_source[item["source_name"] or "Unknown"].append(item)
        for source_name, source_items in filtered_by_source.items():
            lines.append(_source_block(source_name, source_items))

    return lines


def _split_into_messages(lines: list[str]) -> list[str]:
    messages, current = [], ""
    for line in lines:
        candidate = (current + "\n" + line).lstrip("\n")
        if len(candidate) > _TELEGRAM_LIMIT:
            if current:
                messages.append(current)
            current = line
        else:
            current = candidate
    if current:
        messages.append(current)
    return messages


async def send_digest(categories: list[str] | None = None) -> bool:
    items = await get_unsent_items(categories=categories)
    if not items:
        log.info("Digest triggered: no unsent items | filter=%s", categories)
        return False

    blocked_words = await get_blocked_words()
    if blocked_words:
        blocked_patterns = [
            re.compile(rf"(?<!\w){re.escape(b['word'].lower())}", re.UNICODE)
            for b in blocked_words
        ]
        filtered, blocked_items = [], []
        for item in items:
            text_to_check = ((item["summary"] or "") + " " + (item["raw_text"] or "")).lower()
            if any(p.search(text_to_check) for p in blocked_patterns):
                blocked_items.append(item)
            else:
                filtered.append(item)
        if blocked_items:
            blocked_ids = [item["id"] for item in blocked_items]
            await mark_sent(blocked_ids)
            log.info("Blocked %d item(s) by keyword filter", len(blocked_ids))
        items = filtered
        if not items and not blocked_items:
            log.info("Digest triggered: all items filtered by blocked words | filter=%s", categories)
            return False
    else:
        blocked_items = []

    all_categories = await get_categories()
    cat_meta = {
        row["name"]: {"emoji": row["emoji"], "sources": defaultdict(list)}
        for row in all_categories
    }

    for item in items:
        cat = item["category"] or "other"
        if cat not in cat_meta:
            cat_meta[cat] = {"emoji": "📌", "sources": defaultdict(list)}
        source_name = item["source_name"] or "Unknown"
        cat_meta[cat]["sources"][source_name].append(item)

    for cat_name, data in cat_meta.items():
        sources = data["sources"]
        for source_name in list(sources.keys()):
            if len(sources[source_name]) > 1:
                sources[source_name] = await _merge_source_items(sources[source_name])

    for data in cat_meta.values():
        for source_name, source_items in data["sources"].items():
            if len(source_items) > _MAX_ITEMS_PER_SOURCE:
                log.info(
                    "Source '%s': capped at %d (had %d merged items)",
                    source_name, _MAX_ITEMS_PER_SOURCE, len(source_items),
                )
                data["sources"][source_name] = source_items[:_MAX_ITEMS_PER_SOURCE]

    date_str = datetime.now(_get_tz()).strftime("%d %B %Y")
    lines = _build_digest_text(cat_meta, date_str, blocked_items=blocked_items)
    messages = _split_into_messages(lines)

    sent_ids = [item["id"] for item in items]
    sent_count = 0
    failed_count = 0
    first_message_id: int | None = None
    for msg in messages:
        try:
            msg_id = await send_message(msg)
            if first_message_id is None:
                first_message_id = msg_id
            sent_count += 1
        except Exception as exc:
            failed_count = len(messages) - sent_count
            log.error(
                "Digest send failed at message %d/%d (%d message(s) lost, %d items marked anyway): %s",
                sent_count + 1, len(messages), failed_count, len(sent_ids), exc,
            )
            break

    await mark_sent(sent_ids)

    if first_message_id and failed_count == 0:
        prev_id = await get_app_setting("pinned_digest_message_id")
        if prev_id:
            await unpin_message(int(prev_id))
        await pin_message(first_message_id)
        await set_app_setting("pinned_digest_message_id", str(first_message_id))

    status = "ok" if failed_count == 0 else "partial"
    total = len(items) + len(blocked_items)
    await log_digest(total=total, status=status)
    log.info(
        "Digest done: %d items (%d filtered) | %d/%d message(s) sent | filter=%s | status=%s",
        len(items), len(blocked_items), sent_count, len(messages), categories, status,
    )
    return failed_count == 0

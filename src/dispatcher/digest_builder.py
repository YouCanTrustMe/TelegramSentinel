import asyncio
import logging
import re
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from html import escape
from zoneinfo import ZoneInfo

from src.config import settings
from src.db.models import get_app_setting, get_blocked_words, get_categories, get_silent_radar_chats, get_silent_sources, get_unsent_items, log_digest, mark_sent, set_app_setting, update_item_classification
from src.dispatcher.sender import delete_message, edit_message, pin_message, send_message, unpin_message
from src.processor.classifier import ClassificationResult, classify, group_by_topic, is_quota_dead, _wants_no_merge, _wants_no_filter

log = logging.getLogger(__name__)

_digest_lock = asyncio.Lock()
_TELEGRAM_LIMIT = 4000
_MAX_ITEMS_PER_SOURCE = 50
_MEDIA_EMOJI = {"[Photo]": "📷", "[Video]": "🎬", "[GIF]": "🎞️"}


def _progress_bar(done: int, total: int, width: int = 8) -> str:
    filled = round(width * done / total) if total else 0
    return "▓" * filled + "░" * (width - filled)


def _get_tz() -> ZoneInfo:
    return ZoneInfo(settings.digest_timezone)


def _format_item(item: dict) -> str:
    url = item["original_url"] or ""
    summary_text = item["summary"] or ""
    if not summary_text:
        raw = item["raw_text"] or ""
        summary_text = raw[:60].split("\n")[0]

    summary_text = _MEDIA_EMOJI.get(summary_text, summary_text)

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

    item_keys = item.keys()
    suffix = ""

    prefix = f"{hour} · " if hour else ""
    key_phrase = ((item["key_phrase"] if "key_phrase" in item_keys else "") or "").strip()
    if url and key_phrase:
        escaped_url = escape(url, quote=True)
        rest_text = summary_text.strip()
        idx = rest_text.lower().find(key_phrase.lower())
        if idx != -1:
            end = idx + len(key_phrase)
            while end < len(rest_text) and re.match(r"\w", rest_text[end], re.UNICODE):
                end += 1
            anchor_text = rest_text[idx:end]
            before = escape(rest_text[:idx].rstrip())
            after = escape(rest_text[end:].lstrip())
            link = f'<a href="{escaped_url}">{escape(anchor_text)}</a>'
            parts_text = " ".join(p for p in [before, link, after] if p)
            return f'{prefix}{parts_text}{suffix}'
        words = rest_text.split(" ", 1)
        fallback_anchor = escape(words[0])
        fallback_rest = (" " + escape(words[1])) if len(words) > 1 else ""
        return f'{prefix}<a href="{escaped_url}">{fallback_anchor}</a>{fallback_rest}{suffix}'
    if url and summary_text.strip():
        words = summary_text.strip().split(" ", 1)
        anchor = escape(words[0])
        rest = (" " + escape(words[1])) if len(words) > 1 else ""
        escaped_url = escape(url, quote=True)
        return f'{prefix}<a href="{escaped_url}">{anchor}</a>{rest}{suffix}'
    if url:
        escaped_url = escape(url, quote=True)
        return f'{prefix}<a href="{escaped_url}">→</a>{suffix}'
    if summary_text.strip():
        return f"{prefix}{summary}{suffix}"
    return ""


_MERGE_MIN_ITEMS = 4


def _items_as_plain(items: list) -> list[dict]:
    return [
        {
            "summary": item["summary"],
            "key_phrase": item["key_phrase"] if "key_phrase" in item.keys() else "",
            "original_url": item["original_url"],
            "published_at": item["published_at"],
            "raw_text": item["raw_text"],
        }
        for item in items
    ]


async def _merge_source_items(items: list, prompt_extra: str | None = None) -> list[dict]:
    if len(items) < _MERGE_MIN_ITEMS or _wants_no_merge(prompt_extra):
        return _items_as_plain(items)

    if is_quota_dead():
        log.info("Skipping group_by_topic for source: Groq quota dead, returning items as-is")
        return _items_as_plain(items)

    raw_inputs = [{"id": i, "text": item["raw_text"] or ""} for i, item in enumerate(items)]
    try:
        groups = await group_by_topic(raw_inputs, prompt_extra=prompt_extra)
        merged = []
        for g in groups:
            group_items = [items[i] for i in g["ids"]]
            url = next((x["original_url"] for x in group_items if x["original_url"]), None)
            pub = max(
                (x["published_at"] for x in group_items if x["published_at"]),
                default=None,
            )
            summary = g["summary"] or group_items[0]["summary"] or ""
            if not summary:
                raw_fallback = (group_items[0]["raw_text"] or "")[:80].split("\n")[0]
                summary = raw_fallback
            n = len(g["ids"])
            if n > 1:
                summary = f"{summary} · merged {n}"
            merged.append({
                "summary": summary,
                "key_phrase": g.get("key_phrase") or "",
                "original_url": url,
                "published_at": pub,
                "raw_text": None,
            })
        return merged
    except Exception as exc:
        log.warning("Topic merging failed, using original items: %s", exc)
        return _items_as_plain(items)


def _source_block(source_name: str, source_items: list) -> str:
    item_lines = [_format_item(item) for item in source_items]
    item_lines = [l for l in item_lines if l]
    if not item_lines:
        return ""
    header = f"<b>{escape(source_name)}</b>"
    return "<blockquote expandable>" + "\n".join([header] + item_lines) + "</blockquote>"


def _build_digest_text(
    cat_meta: dict,
    date_str: str,
    blocked_items: list | None = None,
    filtered_categories: list[str] | None = None,
    all_categories: list | None = None,
) -> list[str]:
    lines = [f"<b>📋 Digest — {date_str}</b>"]
    if filtered_categories is not None and all_categories:
        cat_info = {r["name"]: r["emoji"] for r in all_categories}
        tags = " · ".join(
            f"{cat_info.get(c, '📌')} {escape(c.capitalize())}"
            for c in filtered_categories
            if c in cat_info
        )
        if tags:
            lines.append(f"<i>{tags}</i>")

    for cat_name, data in cat_meta.items():
        sources = data["sources"]
        if not any(sources.values()):
            continue

        lines.append(f"\n<b>{data['emoji']} {cat_name.capitalize()}</b>")

        for source_name, source_items in sources.items():
            if not source_items:
                continue
            block = _source_block(source_name, source_items)
            if block:
                lines.append(block)

    if blocked_items:
        lines.append("\n<b>🚫 Filtered</b>")
        filtered_by_word: dict[str, list] = defaultdict(list)
        for item in blocked_items:
            word = item.get("blocked_by") or "?"
            filtered_by_word[word].append(item)
        for word, word_items in filtered_by_word.items():
            lines.append(_source_block(escape(word), word_items))

    return lines


async def _build_silent_block() -> str:
    sources = await get_silent_sources(120)
    radar = await get_silent_radar_chats(120)
    if not sources and not radar:
        return ""
    lines = ["\n<b>⏸ Quiet sources</b> (5+ days without new items)"]
    for row in sources:
        hours = row["hours_silent"]
        age = f"{hours // 24}d" if hours is not None else "never"
        lines.append(f"• {escape(row['name'])} [{row['type']}] — {age}")
    for row in radar:
        hours = row["hours_silent"]
        age = f"{hours // 24}d" if hours is not None else "?"
        label = row["title"] or row["chat_ref"]
        lines.append(f"• {escape(label)} [radar] — {age}")
    return "\n".join(lines)


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


async def send_digest(
    categories: list[str] | None = None,
    include_quiet: bool = False,
    status_fn: Callable[[str], Awaitable[None]] | None = None,
) -> bool | None:
    if _digest_lock.locked():
        log.warning("Digest already in progress, skipping duplicate run | filter=%s", categories)
        return None
    async with _digest_lock:
        return await _send_digest_locked(categories, include_quiet, status_fn)


async def _send_digest_locked(
    categories: list[str] | None = None,
    include_quiet: bool = False,
    status_fn: Callable[[str], Awaitable[None]] | None = None,
) -> bool:
    async def _update(text: str) -> None:
        if status_fn:
            try:
                await status_fn(text)
            except Exception:
                pass
        if building_msg_id:
            try:
                await edit_message(building_msg_id, text)
            except Exception:
                pass

    items = await get_unsent_items(categories=categories)
    if not items:
        log.info("Digest triggered: no unsent items | filter=%s", categories)
        return False

    building_msg_id: int | None = None
    if not status_fn:
        try:
            building_msg_id = await send_message("⏳ Building digest...")
        except Exception:
            pass

    _RECLASSIFY_TIMEOUT = 120.0
    empty = [item for item in items if not (item["summary"] or "").strip() and (item["raw_text"] or "").strip()]
    if empty:
        log.info("Re-classifying %d item(s) with empty summary before digest (timeout=%ds)", len(empty), int(_RECLASSIFY_TIMEOUT))
        items = list(items)
        reclassify_start = time.monotonic()
        done = 0
        for i, item in enumerate(items):
            if not (item["summary"] or "").strip() and (item["raw_text"] or "").strip():
                if is_quota_dead():
                    remaining = sum(1 for x in items[i:] if not (x["summary"] or "").strip())
                    log.warning("Re-classify aborted: Groq quota dead, %d items will show as link", remaining)
                    break
                elapsed = time.monotonic() - reclassify_start
                if elapsed > _RECLASSIFY_TIMEOUT:
                    remaining = sum(1 for x in items[i:] if not (x["summary"] or "").strip())
                    log.warning("Re-classify timeout after %.0fs, %d items will show as link", elapsed, remaining)
                    break
                await _update(f"⏳ Re-classifying {done + 1}/{len(empty)}...")
                raw = (item["raw_text"] or "").strip()
                if len(raw) < 15:
                    await update_item_classification(item["id"], raw, "")
                    items[i] = {**dict(item), "summary": raw, "key_phrase": ""}
                    log.info("Short raw_text used as summary for item id=%d", item["id"])
                else:
                    remaining_time = _RECLASSIFY_TIMEOUT - (time.monotonic() - reclassify_start)
                    try:
                        result = await asyncio.wait_for(classify(raw, max_retries=3), timeout=max(5.0, remaining_time))
                    except asyncio.TimeoutError:
                        log.warning("Re-classify timed out on item id=%d, will show as link", item["id"])
                        result = ClassificationResult(summary="")
                    if result.summary:
                        await update_item_classification(item["id"], result.summary, result.key_phrase)
                        items[i] = {**dict(item), "summary": result.summary, "key_phrase": result.key_phrase}
                        log.info("Re-classified item id=%d | summary=%s", item["id"], result.summary)
                    else:
                        log.warning("Re-classify gave up on item id=%d, will show as link", item["id"])
                done += 1

    blocked_words = await get_blocked_words()
    if blocked_words:
        blocked_patterns = []
        for b in blocked_words:
            w = b["word"].lower()
            if w.endswith("*"):
                blocked_patterns.append(re.compile(rf"\b{re.escape(w[:-1])}", re.UNICODE))
            else:
                blocked_patterns.append(re.compile(rf"\b{re.escape(w)}\b", re.UNICODE))
        filtered, blocked_items = [], []
        for item in items:
            if _wants_no_filter(item["source_prompt_extra"] if "source_prompt_extra" in item.keys() else None):
                filtered.append(item)
                continue
            text_to_check = ((item["summary"] or "") + " " + (item["raw_text"] or "")).lower()
            matched_word = next(
                (b["word"] for p, b in zip(blocked_patterns, blocked_words) if p.search(text_to_check)),
                None,
            )
            if matched_word is not None:
                blocked_items.append({**item, "blocked_by": matched_word})
            else:
                filtered.append(item)
        if blocked_items:
            blocked_ids = [item["id"] for item in blocked_items]
            await mark_sent(blocked_ids)
            log.info("Blocked %d item(s) by keyword filter", len(blocked_ids))
        items = filtered
        if not items and not blocked_items:
            log.info("Digest triggered: all items filtered by blocked words | filter=%s", categories)
            if building_msg_id:
                try:
                    await delete_message(building_msg_id)
                except Exception:
                    pass
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

    source_prompt_extra: dict[str, str | None] = {}
    for item in items:
        sname = item["source_name"] or "Unknown"
        if sname not in source_prompt_extra:
            keys = item.keys()
            source_prompt_extra[sname] = item["source_prompt_extra"] if "source_prompt_extra" in keys else None

    sources_to_merge = [
        (cat_name, source_name)
        for cat_name, data in cat_meta.items()
        for source_name, source_items in data["sources"].items()
        if len(source_items) >= _MERGE_MIN_ITEMS and not _wants_no_merge(source_prompt_extra.get(source_name))
    ]
    total = len(sources_to_merge)
    done = 0
    if total:
        await _update(f"⏳ {_progress_bar(0, total)} 0/{total}")
    for cat_name, source_name in sources_to_merge:
        cat_meta[cat_name]["sources"][source_name] = await _merge_source_items(
            cat_meta[cat_name]["sources"][source_name],
            prompt_extra=source_prompt_extra.get(source_name),
        )
        done += 1
        await _update(f"⏳ {_progress_bar(done, total)} {done}/{total} — {source_name}")

    for data in cat_meta.values():
        for source_name, source_items in data["sources"].items():
            if len(source_items) > _MAX_ITEMS_PER_SOURCE:
                log.info(
                    "Source '%s': capped at %d (had %d merged items)",
                    source_name, _MAX_ITEMS_PER_SOURCE, len(source_items),
                )
                data["sources"][source_name] = source_items[:_MAX_ITEMS_PER_SOURCE]

    date_str = datetime.now(_get_tz()).strftime("%d %B %Y")
    lines = _build_digest_text(
        cat_meta,
        date_str,
        blocked_items=blocked_items,
        filtered_categories=categories,
        all_categories=all_categories,
    )
    if include_quiet:
        silent_block = await _build_silent_block()
        if silent_block:
            lines.append(silent_block)
            log.info("Appended quiet-sources block to digest")
    messages = _split_into_messages(lines)

    await _update("⏳ Sending...")
    if building_msg_id:
        try:
            await delete_message(building_msg_id)
        except Exception:
            pass
        building_msg_id = None

    sent_ids = [item["id"] for item in items]
    sent_count = 0
    failed_count = 0
    first_message_id: int | None = None
    for msg in messages:
        try:
            msg_id = await send_message(msg, disable_notification=first_message_id is not None)
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

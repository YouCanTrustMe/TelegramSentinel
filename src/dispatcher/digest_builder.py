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
from src.db.models import get_app_setting, get_blocked_words, get_categories, get_silent_sources, get_unsent_items, get_word_category_map, log_digest, mark_sent, set_app_setting, update_item_classification
from src.dispatcher.sender import delete_message, edit_message, pin_message, send_message, unpin_message
from src.processor.classifier import ClassificationResult, classify, check_blocked_filters, is_quota_dead, _wants_no_merge, _wants_no_filter
from src.processor.cross_dedup import deduplicate, ensure_embeddings
from src.processor.groq_client import format_groq_stats, reset_groq_stats
from src.processor.merge import MERGE_MIN_ITEMS, merge_source_items
from src.media import MEDIA_EMOJI as _MEDIA_EMOJI
from src.util import needs_summary, row_get

log = logging.getLogger(__name__)

_digest_lock = asyncio.Lock()
_TELEGRAM_LIMIT = 4000
# A blockquote is indivisible once built, so cap it under Telegram's 4096 limit
# and split a source across several blocks. Counting raw length is conservative.
_MAX_BLOCK_LEN = 3800
_MAX_ITEMS_PER_SOURCE = 50
_DEFER_MAX_DAYS = 3


def _progress_bar(done: int, total: int, width: int = 8) -> str:
    filled = round(width * done / total) if total else 0
    return "▓" * filled + "░" * (width - filled)


def _get_tz() -> ZoneInfo:
    return ZoneInfo(settings.digest_timezone)


def _ids_of(item) -> list[int]:
    """Item id(s) a rendered line stands for: merged groups carry the ids they
    collapsed, un-merged rows carry their own."""
    keys = item.keys()
    if "_item_ids" in keys:
        return list(item["_item_ids"])
    if "id" in keys:
        return [item["id"]]
    return []


def _format_item(item: dict, dup_links: dict[int, list[tuple[str, str]]] | None = None) -> str:
    """Render an item line, appending clickable source links for any cross-source
    duplicates muted under it."""
    line = _format_item_base(item)
    if not line or not dup_links:
        return line
    links = []
    for iid in _ids_of(item):
        for name, url in dup_links.get(iid, []):
            if url:
                links.append(f'<a href="{escape(url, quote=True)}">{escape(name)}</a>')
    if links:
        return f"{line} ({', '.join(links)})"
    return line


def _format_item_base(item: dict) -> str:
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

    suffix = ""

    prefix = f"{hour} · " if hour else ""
    key_phrase = (row_get(item, "key_phrase", "") or "").strip()
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


def _source_blocks(
    source_name: str,
    source_items: list,
    dup_links: dict[int, list[tuple[str, str]]] | None = None,
) -> list[tuple[str, list[int]]]:
    """Render a source's items into one or more expandable blockquotes, each
    under _MAX_BLOCK_LEN, paired with the item ids they render."""
    rendered: list[tuple[str, list[int]]] = []
    for item in source_items:
        line = _format_item(item, dup_links)
        if line:
            rendered.append((line, _ids_of(item)))
    if not rendered:
        return []

    header = f"<b>{escape(source_name)}</b>"

    def _wrap(lines: list[str]) -> str:
        return "<blockquote expandable>" + "\n".join([header] + lines) + "</blockquote>"

    blocks: list[tuple[str, list[int]]] = []
    cur_lines: list[str] = []
    cur_ids: list[int] = []
    for line, ids in rendered:
        if cur_lines and len(_wrap(cur_lines + [line])) > _MAX_BLOCK_LEN:
            blocks.append((_wrap(cur_lines), cur_ids))
            cur_lines, cur_ids = [line], list(ids)
        else:
            cur_lines.append(line)
            cur_ids = cur_ids + list(ids)
    if cur_lines:
        blocks.append((_wrap(cur_lines), cur_ids))
    return blocks


def _build_digest_text(
    cat_meta: dict,
    date_str: str,
    blocked_items: list | None = None,
    filtered_categories: list[str] | None = None,
    all_categories: list | None = None,
    dup_links: dict[int, list[tuple[str, str]]] | None = None,
) -> list[tuple[str, list[int]]]:
    """Build the digest as (text, item_ids) segments so delivery can be
    confirmed per message. Headers and already-marked blocked items carry no ids."""
    segments: list[tuple[str, list[int]]] = [(f"<b>📋 Digest — {date_str}</b>", [])]
    if filtered_categories is not None and all_categories:
        cat_info = {r["name"]: r["emoji"] for r in all_categories}
        tags = " · ".join(
            f"{cat_info.get(c, '📌')} {escape(c.capitalize())}"
            for c in filtered_categories
            if c in cat_info
        )
        if tags:
            segments.append((f"<i>{tags}</i>", []))

    for cat_name, data in cat_meta.items():
        sources = data["sources"]
        if not any(sources.values()):
            continue

        segments.append((f"\n<b>{data['emoji']} {cat_name.capitalize()}</b>", []))

        for source_name, source_items in sources.items():
            if not source_items:
                continue
            for block_text, block_ids in _source_blocks(source_name, source_items, dup_links):
                segments.append((block_text, block_ids))

    if blocked_items:
        segments.append(("\n<b>🚫 Filtered</b>", []))
        filtered_by_word: dict[str, list] = defaultdict(list)
        for item in blocked_items:
            word = item.get("blocked_by") or "?"
            filtered_by_word[word].append(item)
        for word, word_items in filtered_by_word.items():
            for block_text, _ in _source_blocks(word, word_items):
                segments.append((block_text, []))

    return segments


def _quiet_source_url(row) -> str | None:
    """Clickable link for a quiet source: a Telegram handle becomes a t.me link,
    an RSS feed links straight to its url. None when there is nothing linkable."""
    url = (row["url"] or "").strip()
    if not url:
        return None
    if row["type"] == "telegram" and not url.startswith("http"):
        handle = url.lstrip("@")
        # Public usernames start with a letter; a numeric/empty handle (private
        # chat id) has no usable t.me link, so leave it as plain text.
        return f"https://t.me/{handle}" if handle[:1].isalpha() else None
    return url if url.startswith("http") else None


async def _build_silent_block() -> str:
    sources = await get_silent_sources(120)
    if not sources:
        return ""
    lines = ["<b>⏸ Quiet sources</b> (5+ days without new items)"]
    for row in sources:
        hours = row["hours_silent"]
        age = f"{hours // 24}d" if hours is not None else "never"
        name = escape(row["name"])
        url = _quiet_source_url(row)
        label = f'<a href="{escape(url, quote=True)}">{name}</a>' if url else name
        lines.append(f"• {label} [{row['type']}] — {age}")
    return "<blockquote expandable>" + "\n".join(lines) + "</blockquote>"


def _split_into_messages(segments: list[tuple[str, list[int]]]) -> list[tuple[str, list[int]]]:
    messages: list[tuple[str, list[int]]] = []
    cur_text, cur_ids = "", []
    for text, ids in segments:
        candidate = (cur_text + "\n" + text).lstrip("\n")
        if len(candidate) > _TELEGRAM_LIMIT:
            if cur_text:
                messages.append((cur_text, cur_ids))
            cur_text, cur_ids = text, list(ids)
        else:
            cur_text = candidate
            cur_ids = cur_ids + list(ids)
    if cur_text:
        messages.append((cur_text, cur_ids))
    return messages


async def _discard_building(building_msg_id: int | None) -> None:
    """Best-effort removal of the transient "Building digest..." status message."""
    if building_msg_id:
        try:
            await delete_message(building_msg_id)
        except Exception:
            pass


_RECLASSIFY_TIMEOUT = 120.0


async def _reclassify_empty_summaries(items: list, update: Callable[[str], Awaitable[None]]) -> list:
    """Re-run classification on items with an empty summary but non-empty raw
    text, bounded by a wall-clock timeout and per-model quota. Returns the
    (possibly rebuilt) list; items still empty fall through to _defer_empty_items."""
    empty = [item for item in items if needs_summary(item)]
    if not empty:
        return items
    log.info("Re-classifying %d item(s) with empty summary before digest (timeout=%ds)", len(empty), int(_RECLASSIFY_TIMEOUT))
    items = list(items)
    reclassify_start = time.monotonic()
    done = 0
    for i, item in enumerate(items):
        if needs_summary(item):
            if is_quota_dead(settings.groq_model_classify) and is_quota_dead(settings.groq_model_fallback):
                remaining = sum(1 for x in items[i:] if not (x["summary"] or "").strip())
                log.warning("Re-classify aborted: classify model and fallback both quota dead, %d items will show as link", remaining)
                break
            elapsed = time.monotonic() - reclassify_start
            if elapsed > _RECLASSIFY_TIMEOUT:
                remaining = sum(1 for x in items[i:] if not (x["summary"] or "").strip())
                log.warning("Re-classify timeout after %.0fs, %d items will show as link", elapsed, remaining)
                break
            await update(f"⏳ Re-classifying {done + 1}/{len(empty)}...")
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
    return items


async def _defer_empty_items(items: list) -> tuple[list, int]:
    """Resolve items still empty after re-classify: ones younger than
    _DEFER_MAX_DAYS are deferred for a later retry, older ones get a raw-text
    fallback summary. Returns (kept_items, deferred_count)."""
    now = datetime.now(timezone.utc)
    kept, deferred = [], 0
    for item in items:
        if not needs_summary(item):
            kept.append(item)
            continue
        ts = item["processed_at"] or item["published_at"]
        age_days = None
        if ts:
            try:
                age_days = (now - datetime.fromisoformat(ts)).total_seconds() / 86400
            except ValueError:
                age_days = None
        if age_days is not None and age_days < _DEFER_MAX_DAYS:
            deferred += 1
            continue
        raw = (item["raw_text"] or "").strip()
        fallback = "⚠️ " + raw[:80].split("\n")[0]
        await update_item_classification(item["id"], fallback, "")
        kept.append({**dict(item), "summary": fallback})
    return kept, deferred


async def _apply_semantic_filter(items: list) -> tuple[list, list]:
    """Run the LLM content filter over items that some rule targets. Blocked
    items are marked sent and returned separately for the Filtered section.
    Fail-open: on error the digest goes out unfiltered. Returns (kept, blocked)."""
    filter_rules_rows = await get_blocked_words()
    blocked_items: list = []
    if not filter_rules_rows:
        return items, blocked_items
    try:
        rules = [r["rule"] for r in filter_rules_rows]
        scope_map = await get_word_category_map()
        # Aligned with `rules`: None means the rule applies to every category.
        rule_scopes = [scope_map.get(r["id"]) or None for r in filter_rules_rows]

        def _has_applicable_rule(cat: str) -> bool:
            return any(scope is None or cat in scope for scope in rule_scopes)

        filterable, no_filter = [], []
        for item in items:
            prompt_extra = row_get(item, "source_prompt_extra")
            cat = item["category"] or "other"
            # Skip the LLM filter for items no rule targets (saves tokens) and for opted-out sources.
            if _wants_no_filter(prompt_extra) or not _has_applicable_rule(cat):
                no_filter.append(item)
            else:
                filterable.append(item)
        check_input = [
            {
                "id": item["id"],
                "text": (item["summary"] or "") + " " + (item["raw_text"] or ""),
                "source": item["source_name"] or "unknown",
                "category": item["category"] or "other",
            }
            for item in filterable
        ]
        blocked_map = await check_blocked_filters(check_input, rules, rule_scopes)
        if blocked_map:
            for item in filterable:
                matched_rule = blocked_map.get(item["id"])
                if matched_rule is not None:
                    blocked_items.append({**item, "blocked_by": matched_rule})
                    log.info("Blocked item id=%d | rule=%r | summary=%s", item["id"], matched_rule, (item["summary"] or "")[:80])
            await mark_sent([item["id"] for item in blocked_items])
            log.info("Blocked %d item(s) by semantic filter", len(blocked_items))
        items = no_filter + [item for item in filterable if item["id"] not in blocked_map]
    except Exception:
        log.exception("Semantic filter failed, sending digest unfiltered")
        blocked_items = []
    return items, blocked_items


async def _run_within_source_merge(
    cat_meta: dict,
    source_prompt_extra: dict,
    vectors: dict,
    update: Callable[[str], Awaitable[None]],
) -> None:
    """Merge same-event items within each source in place, with progress updates."""
    # Embedding-based merge clusters cheaply, so it is worth running from 2 items;
    # the old LLM path only paid off in bulk (>= MERGE_MIN_ITEMS).
    merge_min = 2 if settings.merge_via_embeddings else MERGE_MIN_ITEMS
    sources_to_merge = [
        (cat_name, source_name)
        for cat_name, data in cat_meta.items()
        for source_name, source_items in data["sources"].items()
        if len(source_items) >= merge_min and not _wants_no_merge(source_prompt_extra.get(source_name))
    ]
    merge_stats = {"clusters": 0, "llm": 0, "near_dup": 0}
    merge_total = len(sources_to_merge)
    merge_done = 0
    if merge_total:
        await update(f"⏳ {_progress_bar(0, merge_total)} 0/{merge_total}")
    for cat_name, source_name in sources_to_merge:
        cat_meta[cat_name]["sources"][source_name] = await merge_source_items(
            cat_meta[cat_name]["sources"][source_name],
            prompt_extra=source_prompt_extra.get(source_name),
            vectors=vectors,
            stats=merge_stats,
        )
        merge_done += 1
        await update(f"⏳ {_progress_bar(merge_done, merge_total)} {merge_done}/{merge_total} — {source_name}")
    if merge_total:
        log.info(
            "Within-source merge: sources=%d clusters_merged=%d llm_calls=%d near-dup-skip=%d | mode=%s",
            merge_total, merge_stats["clusters"], merge_stats["llm"], merge_stats["near_dup"],
            "embeddings" if settings.merge_via_embeddings else "group_by_topic",
        )


async def _deliver(messages: list[tuple[str, list[int]]]) -> tuple[int, int, int, bool]:
    """Send each digest message, marking only items whose message actually
    reached Telegram (a mid-batch failure leaves the rest sent=0 for the next
    digest — no duplicates, no silent loss), then pin the first message.
    Returns (sent_count, total_messages, confirmed_count, failed)."""
    total_messages = len(messages)
    confirmed_ids: list[int] = []
    sent_count = 0
    first_message_id: int | None = None
    for msg_text, msg_ids in messages:
        try:
            msg_id = await send_message(msg_text, disable_notification=first_message_id is not None)
            if first_message_id is None:
                first_message_id = msg_id
            confirmed_ids.extend(msg_ids)
            sent_count += 1
        except Exception as exc:
            lost = total_messages - sent_count
            log.error(
                "Digest send failed at message %d/%d (%d message(s) and their items left unsent for retry): %s",
                sent_count + 1, total_messages, lost, exc,
            )
            break

    failed = sent_count < total_messages
    if confirmed_ids:
        await mark_sent(confirmed_ids)

    if first_message_id and not failed:
        prev_id = await get_app_setting("pinned_digest_message_id")
        if prev_id:
            await unpin_message(int(prev_id))
        await pin_message(first_message_id)
        await set_app_setting("pinned_digest_message_id", str(first_message_id))

    return sent_count, total_messages, len(confirmed_ids), failed


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

    items = await _reclassify_empty_summaries(items, _update)

    items, deferred = await _defer_empty_items(items)
    if deferred:
        log.info("Deferred %d empty item(s) past digest (younger than %dd), will retry later", deferred, _DEFER_MAX_DAYS)
    if not items:
        log.info("Digest triggered: nothing to send after deferring %d empty item(s) | filter=%s", deferred, categories)
        await _discard_building(building_msg_id)
        return False

    items, blocked_items = await _apply_semantic_filter(items)
    if not items:
        log.info("Digest triggered: all items filtered by semantic filter | filter=%s", categories)
        await _discard_building(building_msg_id)
        return False

    # Embeddings are computed once here and shared by cross-source dedup and
    # within-source merge (both cluster on the same vectors).
    vectors: dict = {}
    if settings.dedup_enabled or settings.merge_via_embeddings:
        vectors = await ensure_embeddings(items)

    dup_link_map: dict[int, list[tuple[str, str]]] = {}
    if settings.dedup_enabled:
        items, dup_link_map = await deduplicate(items, vectors)
        if not items:
            log.info("Digest triggered: all items deduplicated away | filter=%s", categories)
            await _discard_building(building_msg_id)
            return False

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
            source_prompt_extra[sname] = row_get(item, "source_prompt_extra")

    await _run_within_source_merge(cat_meta, source_prompt_extra, vectors, _update)

    for data in cat_meta.values():
        for source_name, source_items in data["sources"].items():
            if len(source_items) > _MAX_ITEMS_PER_SOURCE:
                log.info(
                    "Source '%s': capped at %d (had %d merged items)",
                    source_name, _MAX_ITEMS_PER_SOURCE, len(source_items),
                )
                data["sources"][source_name] = source_items[:_MAX_ITEMS_PER_SOURCE]

    date_str = datetime.now(_get_tz()).strftime("%d %B %Y")
    segments = _build_digest_text(
        cat_meta,
        date_str,
        blocked_items=blocked_items,
        filtered_categories=categories,
        all_categories=all_categories,
        dup_links=dup_link_map,
    )
    if include_quiet:
        silent_block = await _build_silent_block()
        if silent_block:
            segments.append((silent_block, []))
            log.info("Appended quiet-sources block to digest")
    messages = _split_into_messages(segments)

    await _update("⏳ Sending...")
    await _discard_building(building_msg_id)
    building_msg_id = None

    sent_count, total_messages, confirmed_count, failed = await _deliver(messages)

    status = "ok" if not failed else "partial"
    logged_total = len(items) + len(blocked_items)
    await log_digest(total=logged_total, status=status)
    log.info(
        "Digest done: %d items (%d filtered) | %d/%d message(s) sent | %d items confirmed | filter=%s | status=%s",
        len(items), len(blocked_items), sent_count, total_messages, confirmed_count, categories, status,
    )
    log.info(format_groq_stats())
    reset_groq_stats()
    return not failed

import asyncio
import logging
import re
from dataclasses import dataclass, field

from src.config import settings
from src.processor.llm.llm_client import llm_json, is_task_dead
from src.processor.llm.prompts import (
    _SYSTEM_PROMPT,
    _BATCH_SYSTEM_PROMPT,
    _MULTI_SYSTEM_PROMPT,
    _TRANSLATE_ONLY_PROMPT,
    _FILTER_SYSTEM_PROMPT,
)
from src.common.util import needs_summary

log = logging.getLogger(__name__)

_MEDIA_PREFIX_RE = re.compile(r"^\[(?:Photo|Video|Video note|Audio|Voice|Doc|Document|Sticker|GIF|Animation|Media)\]\s*", re.IGNORECASE)
_TRIVIAL_MAX_LEN = 60

# Only the first N chars of a post are fed to the model; longer posts are
# truncated, so their summary covers just the beginning.
_SINGLE_INPUT_CAP = 1500
_BATCH_INPUT_CAP = 700
_BIG_NEWS_MARK = "…"
_CLASSIFY_CHUNK = 25


def _strip_media_prefix(text: str) -> str:
    return _MEDIA_PREFIX_RE.sub("", text).strip()


def _mark_big(summary: str, text: str, cap: int) -> str:
    """Append an ellipsis when the source text was truncated before summarising."""
    if summary and len(text) > cap and not summary.rstrip().endswith(_BIG_NEWS_MARK):
        return summary.rstrip() + " " + _BIG_NEWS_MARK
    return summary


@dataclass
class ClassificationResult:
    summary: str
    key_phrase: str = field(default="")


async def classify(text: str, prompt_extra: str | None = None, max_retries: int = 5) -> ClassificationResult:
    stripped = _strip_media_prefix(text)
    # Short text is its own summary only when it is already Ukrainian (or translation
    # is disabled); a short non-Ukrainian post still needs the model to translate it.
    if len(stripped) < _TRIVIAL_MAX_LEN and (_wants_no_translate(prompt_extra) or _looks_ukrainian(stripped)):
        log.debug("classify: trivial Ukrainian/short text (%d chars after strip), using raw as summary", len(stripped))
        return ClassificationResult(summary=text.strip(), key_phrase="")

    system = _SYSTEM_PROMPT
    if prompt_extra:
        system = f"{_SYSTEM_PROMPT}\n\nAdditional instructions: {prompt_extra}"

    data = await llm_json(
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": text[:_SINGLE_INPUT_CAP]},
        ],
        max_retries=max_retries,
        task="classify",
    )
    result = ClassificationResult(
        summary=data.get("summary", ""),
        key_phrase=data.get("key_phrase", ""),
    )
    if not _wants_no_translate(prompt_extra):
        result.summary, result.key_phrase = await _ensure_ukrainian(result.summary, result.key_phrase)
    result.summary = _mark_big(result.summary, text, _SINGLE_INPUT_CAP)
    log.debug("Classified: %s | key=%s", result.summary, result.key_phrase)
    return result


async def classify_batch(items: list[dict]) -> dict[int, ClassificationResult]:
    """
    items: list of {"id": int, "text": str}
    Returns: {id: ClassificationResult} for items the model returned.
    Missing ids are left for the caller to fall back on.
    """
    if not items:
        return {}
    text_by_id = {item["id"]: item["text"] or "" for item in items}
    numbered = "\n".join(f"{item['id']}: {item['text'][:_BATCH_INPUT_CAP]}" for item in items)
    data = await llm_json(
        messages=[
            {"role": "system", "content": _MULTI_SYSTEM_PROMPT},
            {"role": "user", "content": numbered},
        ],
        max_retries=3,
        task="batch",
    )
    out: dict[int, ClassificationResult] = {}
    for row in data.get("items", []):
        try:
            rid = int(row["id"])
        except (KeyError, TypeError, ValueError):
            continue
        summary, key_phrase = await _ensure_ukrainian(row.get("summary", "") or "", row.get("key_phrase", "") or "")
        out[rid] = ClassificationResult(
            summary=_mark_big(summary, text_by_id.get(rid, ""), _BATCH_INPUT_CAP),
            key_phrase=key_phrase,
        )
    log.debug("Batch classified %d/%d items", len(out), len(items))
    return out


_NO_MERGE_KEYWORDS = ("no merge", "no_merge", "не мерджити", "не об'єднувати", "не объединять", "окремо", "separate")
_NO_FILTER_KEYWORDS = ("no filter", "no_filter", "не фільтрувати", "не блокувати", "не фільтр", "bypass filter")
_NO_TRANSLATE_KEYWORDS = ("no translate", "no_translate", "no translation", "без перекладу", "не перекладати", "keep original language")


def _wants_no_merge(prompt_extra: str | None) -> bool:
    if not prompt_extra:
        return False
    lower = prompt_extra.lower()
    return any(kw in lower for kw in _NO_MERGE_KEYWORDS)


def _wants_no_filter(prompt_extra: str | None) -> bool:
    if not prompt_extra:
        return False
    lower = prompt_extra.lower()
    return any(kw in lower for kw in _NO_FILTER_KEYWORDS)


def _wants_no_translate(prompt_extra: str | None) -> bool:
    if not prompt_extra:
        return False
    lower = prompt_extra.lower()
    return any(kw in lower for kw in _NO_TRANSLATE_KEYWORDS)


def _looks_ukrainian(summary: str) -> bool:
    """True if the summary's alphabetic content is mostly Cyrillic (i.e. actually translated)."""
    if not summary:
        return True
    letters = [c for c in summary if c.isalpha()]
    if len(letters) < 4:
        return True
    cyrillic = sum(1 for c in letters if "Ѐ" <= c <= "ӿ")
    return cyrillic / len(letters) >= 0.4


async def _ensure_ukrainian(summary: str, key_phrase: str) -> tuple[str, str]:
    """If `summary` is not Ukrainian, re-translate it on the reliable batch model.
    Shared by the single, batch and grouping paths so a non-Ukrainian summary never
    reaches the digest. Returns the (possibly fixed) summary and key_phrase."""
    if not summary or _looks_ukrainian(summary):
        return summary, key_phrase
    log.info("Summary not in Ukrainian, re-translating | got=%s", summary[:80])
    data = await llm_json(
        messages=[
            {"role": "system", "content": _TRANSLATE_ONLY_PROMPT},
            {"role": "user", "content": summary},
        ],
        max_retries=2,
        task="translate",
    )
    new_summary = data.get("summary", "") or ""
    if new_summary and _looks_ukrainian(new_summary):
        log.info("Re-translated to Ukrainian | summary=%s", new_summary[:80])
        return new_summary, (data.get("key_phrase", "") or key_phrase)
    return summary, key_phrase


async def group_by_topic(items: list[dict], prompt_extra: str | None = None) -> list[dict]:
    """
    items: list of {"id": int, "text": str}
    Returns: list of {"ids": [int, ...], "summary": str, "key_phrase": str}
    Falls back to one group per item on error.
    """
    all_ids = {item["id"] for item in items}
    text_by_id = {item["id"]: item["text"] or "" for item in items}
    translate = not _wants_no_translate(prompt_extra)

    if _wants_no_merge(prompt_extra):
        log.info("group_by_topic: no-merge instruction in prompt_extra, using multi-summarise")
        numbered = "\n".join(f"{item['id']}: {item['text'][:_BATCH_INPUT_CAP]}" for item in items)
        system = _MULTI_SYSTEM_PROMPT
        if prompt_extra:
            system = f"{_MULTI_SYSTEM_PROMPT}\n\nAdditional instructions: {prompt_extra}"
        data = await llm_json(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": numbered},
            ],
            max_retries=3,
            task="batch",
        )
        result = []
        covered: set[int] = set()
        for row in data.get("items", []):
            try:
                rid = int(row["id"])
            except (KeyError, TypeError, ValueError):
                continue
            covered.add(rid)
            summary, key_phrase = row.get("summary", "") or "", row.get("key_phrase", "") or ""
            if translate:
                summary, key_phrase = await _ensure_ukrainian(summary, key_phrase)
            result.append({
                "ids": [rid],
                "summary": _mark_big(summary, text_by_id.get(rid, ""), _BATCH_INPUT_CAP),
                "key_phrase": key_phrase,
            })
        for mid in all_ids - covered:
            result.append({"ids": [mid], "summary": "", "key_phrase": ""})
        log.debug("No-merge summarised %d items into %d entries", len(items), len(result))
        return result

    numbered = "\n".join(f"{item['id']}: {item['text'][:_BATCH_INPUT_CAP]}" for item in items)
    system = _BATCH_SYSTEM_PROMPT
    if prompt_extra and not _wants_no_merge(prompt_extra):
        system = f"{_BATCH_SYSTEM_PROMPT}\n\nAdditional instructions: {prompt_extra}"

    data = await llm_json(
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": numbered},
        ],
        max_retries=3,
        task="group",
    )
    groups = data.get("groups", [])
    if not groups:
        log.warning("Batch grouping returned empty, falling back to individual items")
        return [{"ids": [item["id"]], "summary": "", "key_phrase": ""} for item in items]

    result = []
    covered_ids: set[int] = set()
    for g in groups:
        # The LLM occasionally hallucinates an id that was never an input, or emits
        # a group with empty `ids`. Keep only real input ids and drop now-empty
        # groups so callers never get a phantom/empty group (an empty one used to
        # crash within-source merge at max() over an empty cluster).
        ids = []
        for raw in g.get("ids", []):
            try:
                v = int(raw)
            except (TypeError, ValueError):
                continue
            if v in all_ids:
                ids.append(v)
        if not ids:
            continue
        for i in ids:
            covered_ids.add(i)
        summary = g.get("summary", "") or ""
        key_phrase = g.get("key_phrase", "") or ""
        if translate:
            summary, key_phrase = await _ensure_ukrainian(summary, key_phrase)
        if any(len(text_by_id.get(i, "")) > _BATCH_INPUT_CAP for i in ids):
            summary = _mark_big(summary, "x" * (_BATCH_INPUT_CAP + 1), _BATCH_INPUT_CAP)
        result.append({
            "ids": ids,
            "summary": summary,
            "key_phrase": key_phrase,
        })

    missing = all_ids - covered_ids
    if missing:
        # Benign + self-healing: a dropped id is re-added as its own group. Callers
        # of the "group" task (within-source merge, B1 dedup confirm) keep the
        # item's original summary / use co-membership only, so nothing degrades —
        # hence INFO, not WARNING.
        log.info("group_by_topic: %d item(s) missing from AI output, re-added as singletons: %s", len(missing), missing)
        for mid in missing:
            result.append({"ids": [mid], "summary": "", "key_phrase": ""})

    log.debug("Grouped %d items into %d groups", len(items), len(result))
    return result


_CLASSIFY_MAX_ATTEMPTS = 3


async def classify_pending_items(limit: int = 3) -> None:
    from src.db.models import (
        get_sent_empty_items,
        get_unsent_items,
        increment_classify_attempts,
        update_item_classification,
    )

    # (id, summary) of items summarised this run; embedded at the end so their
    # vectors land in the DB while the queue is shallow, off the digest's critical
    # path. Otherwise every item is embedded in one burst at digest time, which is
    # what hammers the Gemini quota on big digests.
    classified: list[tuple[int, str]] = []

    def _split(rows: list) -> tuple[list, list]:
        short, long_items = [], []
        for item in rows:
            raw = (item["raw_text"] or "").strip()
            target = short if len(_strip_media_prefix(raw)) < _TRIVIAL_MAX_LEN else long_items
            target.append((item, raw))
        return short, long_items

    async def _classify_store(batch: list, label: str) -> int:
        results = await classify_batch([{"id": item["id"], "text": raw} for item, raw in batch])
        done = 0
        for item, raw in batch:
            result = results.get(item["id"])
            if result and result.summary:
                await update_item_classification(item["id"], result.summary, result.key_phrase)
                classified.append((item["id"], result.summary))
                log.info("%s: item id=%d | summary=%s", label, item["id"], result.summary)
                done += 1
            else:
                attempts = await increment_classify_attempts(item["id"])
                if attempts >= _CLASSIFY_MAX_ATTEMPTS:
                    fallback = "⚠️ " + (raw or "")[:80].split("\n")[0]
                    await update_item_classification(item["id"], fallback, "")
                    log.info("%s: gave up on item id=%d after %d attempts, using fallback", label, item["id"], attempts)
                else:
                    log.debug("%s: no result for item id=%d (attempt %d/%d)", label, item["id"], attempts, _CLASSIFY_MAX_ATTEMPTS)
        return done

    items = await get_unsent_items()
    pending = [item for item in items if needs_summary(item)]
    short, long_items = _split(pending)
    for item, raw in short:
        await update_item_classification(item["id"], raw, "")
        classified.append((item["id"], raw))
    if short:
        log.info("Background classify: %d short text(s) used as summary", len(short))

    both_dead = is_task_dead("batch")
    if both_dead:
        if long_items:
            log.info("Background classify: batch model and fallback both quota dead, skipping %d long items", len(long_items))
    elif long_items:
        batch = long_items[:limit]
        total_classified = 0
        for i in range(0, len(batch), _CLASSIFY_CHUNK):
            total_classified += await _classify_store(batch[i:i + _CLASSIFY_CHUNK], "Background classify")
        log.info("Background classify done: %d/%d classified (%d chunk(s))", total_classified, len(batch), -(-len(batch) // _CLASSIFY_CHUNK))
        # leave backfill for a run when the live queue is clear
    else:
        # Live queue empty + quota alive → backfill already-sent items still empty
        # (e.g. bootstrap residue frozen before classification). Small batch only.
        backlog = await get_sent_empty_items(limit)
        if not backlog:
            log.debug("Background classify: no pending items, nothing to backfill")
        else:
            bshort, blong = _split(backlog)
            for item, raw in bshort:
                await update_item_classification(item["id"], raw, "")
                classified.append((item["id"], raw))
            if blong:
                filled = await _classify_store(blong, "Background backfill")
                log.info("Background backfill: %d/%d sent-empty items re-classified (%d short)", filled, len(blong), len(bshort))
            elif bshort:
                log.info("Background backfill: %d short sent-empty items filled", len(bshort))

    await _embed_classified(classified)


async def _embed_classified(pairs: list[tuple[int, str]]) -> None:
    """Pre-compute and persist embeddings for freshly summarised items so the
    digest finds them already cached instead of embedding everything at once.
    Fail-open: embedding errors never block classification. Imported lazily —
    cross_dedup imports this module, so a top-level import would be circular."""
    if not pairs or not (settings.dedup_enabled or settings.merge_via_embeddings):
        return
    from src.processor.dedup.cross_dedup import ensure_embeddings

    try:
        await ensure_embeddings([{"id": iid, "summary": summary} for iid, summary in pairs])
        log.debug("Pre-embedded %d freshly classified item(s)", len(pairs))
    except Exception:
        log.exception("Pre-embed after classify failed (digest will embed instead)")


_FILTER_BLOCK_THRESHOLD = 8

# Items per filter call. Measured 2026-09-03 on a 120-item prod feed batch: at 25 the
# model caught 3 of 6 real air-raid posts, at 10 it caught 6 of 6 with no new false
# block — recall falls off with batch size, not with the wording of the rule. Chunks
# run concurrently (bounded) so the extra calls do not stretch the digest build.
_FILTER_CHUNK = 10
_FILTER_CONCURRENCY = 4


async def check_blocked_filters(
    items: list[dict],
    rules: list[str],
    rule_scopes: list[set[str] | None] | None = None,
) -> dict[int, str]:
    """Check items against semantic filter rules via LLM.

    items: list of {"id": int, "text": str, "source": str, "category": str}
    rules: list of rule description strings
    rule_scopes: aligned with `rules`; each entry is the set of categories a rule
        applies to, or None for "all categories". A blocked match is discarded if
        the rule does not cover the item's category (guards against model drift).
    Returns: {item_id: matched_rule_text} for items that should be blocked.
    Returns {} on quota exhaustion or error (pass-through, no blocking).
    """
    if not items or not rules:
        return {}

    item_category = {item["id"]: (item.get("category") or "other") for item in items}
    scopes = rule_scopes if rule_scopes is not None else [None] * len(rules)
    result: dict[int, str] = {}
    # Per call, not module level: an asyncio.Semaphore binds to the first loop that
    # contends on it and then raises in any other one.
    slots = asyncio.Semaphore(_FILTER_CONCURRENCY)

    async def check_chunk(chunk: list[dict]) -> dict | None:
        # Show the model only rules that can apply to the categories in this chunk
        # (items are category-ordered, so a chunk is usually one category): fewer
        # input tokens and fewer chances to mis-match an irrelevant rule. Original
        # rule indices are preserved so rule_scopes/rules stay aligned for validation.
        chunk_cats = {item_category[item["id"]] for item in chunk}
        applicable = [j for j, sc in enumerate(scopes) if sc is None or (sc & chunk_cats)]
        if not applicable:
            return None
        numbered_rules = "\n".join(f"{j}. {rules[j]}" for j in applicable)
        numbered_items = "\n".join(
            f"{item['id']} [{item.get('source', '?')}/{item.get('category', '?')}]: {(item['text'] or '')[:150]}"
            for item in chunk
        )
        user_msg = f"Filter rules:\n{numbered_rules}\n\nItems:\n{numbered_items}"
        async with slots:
            return await llm_json(
                messages=[
                    {"role": "system", "content": _FILTER_SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                max_retries=3,
                task="filter",
            )

    chunks = [items[i:i + _FILTER_CHUNK] for i in range(0, len(items), _FILTER_CHUNK)]
    answers = await asyncio.gather(*(check_chunk(c) for c in chunks), return_exceptions=True)
    for data in answers:
        if isinstance(data, asyncio.CancelledError):
            raise data  # a cancelled digest must unwind, not carry on filtering
        if isinstance(data, Exception):
            log.warning("Filter: chunk failed, its items pass through unfiltered: %s", data)
            continue
        if not data or not isinstance(data.get("blocked"), list):
            continue
        for entry in data["blocked"]:
            item_id = entry.get("id")
            rule_idx = entry.get("rule")
            confidence = entry.get("confidence", 10)
            scope = rule_scopes[rule_idx] if (rule_scopes and isinstance(rule_idx, int) and 0 <= rule_idx < len(rule_scopes)) else None
            out_of_scope = bool(scope) and item_category.get(item_id) not in scope
            if (
                isinstance(item_id, int)
                and isinstance(rule_idx, int)
                and 0 <= rule_idx < len(rules)
                and isinstance(confidence, int)
                and confidence >= _FILTER_BLOCK_THRESHOLD
                and not out_of_scope
            ):
                result[item_id] = rules[rule_idx]
                log.info("Filter: blocked item id=%d | rule=%r | confidence=%d", item_id, rules[rule_idx], confidence)
            elif out_of_scope and isinstance(item_id, int) and isinstance(rule_idx, int) and 0 <= rule_idx < len(rules):
                log.info("Filter: kept item id=%d | rule=%r matched but out of scope for category=%s", item_id, rules[rule_idx], item_category.get(item_id))
            elif isinstance(item_id, int) and isinstance(rule_idx, int) and 0 <= rule_idx < len(rules):
                log.info("Filter: kept item id=%d | rule=%r | confidence=%d (below threshold %d)", item_id, rules[rule_idx], confidence, _FILTER_BLOCK_THRESHOLD)
    return result

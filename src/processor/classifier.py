import logging
import re
from dataclasses import dataclass, field

from src.config import settings
from src.processor.groq_client import groq_json, is_quota_dead

log = logging.getLogger(__name__)

_MEDIA_PREFIX_RE = re.compile(r"^\[(?:Photo|Video|Video note|Audio|Voice|Doc|Document|Sticker|GIF|Animation|Media)\]\s*", re.IGNORECASE)
_TRIVIAL_MAX_LEN = 60

# Only the first N chars of a post are fed to the model; longer posts are
# truncated, so their summary covers just the beginning.
_SINGLE_INPUT_CAP = 1500
_BATCH_INPUT_CAP = 700
_BIG_NEWS_MARK = "…"


def _strip_media_prefix(text: str) -> str:
    return _MEDIA_PREFIX_RE.sub("", text).strip()


def _mark_big(summary: str, text: str, cap: int) -> str:
    """Append an ellipsis when the source text was truncated before summarising."""
    if summary and len(text) > cap and not summary.rstrip().endswith(_BIG_NEWS_MARK):
        return summary.rstrip() + " " + _BIG_NEWS_MARK
    return summary


_TRANSLATE_RULE = """LANGUAGE RULE — STRICT: summary MUST be in Ukrainian (Cyrillic). If the source text is in English, Croatian, Polish, Czech, Serbian, Russian, German or any other non-Ukrainian language, TRANSLATE it to Ukrainian. Never copy the original language verbatim — even if the language looks similar to Ukrainian (Croatian, Polish, Russian). The only Latin-letter tokens allowed in the summary are proper nouns kept in their original form (Bitcoin, Tesla, Zagreb, BOSQAR INVEST, Trump). All verbs, nouns, adjectives and connectors must be Ukrainian.
Examples:
  EN: "Bitcoin price drops 8% after Fed rate hike" → "Bitcoin впав на 8% після рішення ФРС підвищити ставку"
  HR: "Zagreb Mayor announced measures to regulate alcohol sales" → "Мер Загреба оголосив заходи з регулювання продажу алкоголю"
  RU: "Президент подписал указ о повышении налогов" → "Президент підписав указ про підвищення податків"
"""

_SYSTEM_PROMPT = f"""Summarize news for a Ukrainian digest. Output JSON only.

{_TRANSLATE_RULE}
summary: up to 15 words for simple news; up to 25 words when the event has multiple key details (numbers, names, consequences). Start with the key entity (person, org, asset, place). Strong verb. Keep all numbers and names exact. Do not abbreviate proper nouns. Never start with: повідомляється, стало відомо, з'явилась інформація, відбулась подія, автор, допис, пост, розповідає, пише.

key_phrase: 1-3 words, best anchor for the link. Priority: person > org > asset ticker > action phrase > location. Use a generic Ukrainian city only if nothing more distinctive exists. Never: автор, допис, інформація, подія, новина.

Respond ONLY with JSON: {{"summary": "...", "key_phrase": "..."}}"""

_BATCH_SYSTEM_PROMPT = f"""Group news items from one source by event, then summarize each group in Ukrainian. Output JSON only.

Items are numbered from 0. Every item MUST appear in exactly one group — no item may be omitted or duplicated.

MERGE RULE: merge ONLY items that describe THE SAME SPECIFIC EVENT with new developments (same attack, same trial, same announcement, same person's statement on same day). Do NOT merge items that are merely about the same topic, person, or organisation if they are different events. WHEN IN DOUBT — KEEP SEPARATE. Never merge more than 3 items into one group; if 4+ items look related, split them into multiple groups of 2-3.
Examples of correct merges: "Air alert in Kyiv" + "All-clear in Kyiv" = one group. "Zelensky signed decree X" + "Details of decree X released" = one group.
Examples of wrong merges: "OPEC raises output" + "Saudi Arabia oil strategy" = separate groups. "Trump raised tariffs" + "EU responds to tariffs" = separate groups. Two separate Bitcoin price-action posts on the same day = separate groups (different events even if same asset).

{_TRANSLATE_RULE}
Per group:
- summary: Single item: up to 20 words. Merged (2-3 items): up to 35 words — include the key development from each merged item. Start with key entity (person, org, asset, place). Strong verb. Keep all numbers and names exact. Never start with: повідомляється, стало відомо, автор, допис, пост.
- key_phrase: 1-3 words. Priority: person > org > asset > action > location. Never: автор, допис, інформація, подія.

Respond ONLY with JSON: {{"groups": [{{"ids": [0], "summary": "...", "key_phrase": "..."}}]}}"""

_MULTI_SYSTEM_PROMPT = f"""Summarize each news item separately in Ukrainian. Output JSON only.

Items are numbered from 0. Produce exactly one entry per input id. Do NOT merge items.

{_TRANSLATE_RULE}
Per item:
- summary: Up to 20 words; up to 25 words for events with multiple key details. Start with the key entity (person, org, asset, place). Strong verb. Keep all numbers and names exact. Do not abbreviate proper nouns. Never start with: повідомляється, стало відомо, автор, допис, пост.
- key_phrase: 1-3 words. Priority: person > org > asset > action > location. Generic city only if nothing better. Never: автор, допис, інформація, подія.

Respond ONLY with JSON: {{"items": [{{"id": 0, "summary": "...", "key_phrase": "..."}}]}}"""


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

    data = await groq_json(
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": text[:_SINGLE_INPUT_CAP]},
        ],
        max_retries=max_retries,
        model=settings.groq_model_classify,
        fallback_model=settings.groq_model_fallback,
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
    data = await groq_json(
        messages=[
            {"role": "system", "content": _MULTI_SYSTEM_PROMPT},
            {"role": "user", "content": numbered},
        ],
        max_retries=3,
        model=settings.groq_model_batch,
        fallback_model=settings.groq_model_fallback,
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


_TRANSLATE_ONLY_PROMPT = (
    "Translate the given text into Ukrainian. Return JSON only: "
    "{\"summary\": \"...\", \"key_phrase\": \"...\"}. Summary must be Cyrillic Ukrainian, "
    "up to 20 words, keep proper nouns and numbers exact. key_phrase: 1-3 words."
)


async def _ensure_ukrainian(summary: str, key_phrase: str) -> tuple[str, str]:
    """If `summary` is not Ukrainian, re-translate it on the reliable batch model.
    Shared by the single, batch and grouping paths so a non-Ukrainian summary never
    reaches the digest. Returns the (possibly fixed) summary and key_phrase."""
    if not summary or _looks_ukrainian(summary):
        return summary, key_phrase
    log.info("Summary not in Ukrainian, re-translating | got=%s", summary[:80])
    data = await groq_json(
        messages=[
            {"role": "system", "content": _TRANSLATE_ONLY_PROMPT},
            {"role": "user", "content": summary},
        ],
        max_retries=2,
        model=settings.groq_model_batch,
        fallback_model=settings.groq_model_fallback,
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
        data = await groq_json(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": numbered},
            ],
            max_retries=3,
            model=settings.groq_model_batch,
            fallback_model=settings.groq_model_fallback,
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

    data = await groq_json(
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": numbered},
        ],
        max_retries=3,
        model=settings.groq_model_batch,
        fallback_model=settings.groq_model_fallback,
    )
    groups = data.get("groups", [])
    if not groups:
        log.warning("Batch grouping returned empty, falling back to individual items")
        return [{"ids": [item["id"]], "summary": "", "key_phrase": ""} for item in items]

    result = []
    covered_ids: set[int] = set()
    for g in groups:
        ids = [int(i) for i in g["ids"]]
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
        log.warning("group_by_topic: %d item(s) missing from AI output, adding as singletons: %s", len(missing), missing)
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
    pending = [
        item for item in items
        if not (item["summary"] or "").strip() and (item["raw_text"] or "").strip()
    ]
    short, long_items = _split(pending)
    for item, raw in short:
        await update_item_classification(item["id"], raw, "")
    if short:
        log.info("Background classify: %d short text(s) used as summary", len(short))

    if is_quota_dead(settings.groq_model_batch) and is_quota_dead(settings.groq_model_fallback):
        if long_items:
            log.info("Background classify: batch model and fallback both quota dead, skipping %d long items", len(long_items))
        return

    if long_items:
        batch = long_items[:limit]
        log.info("Background classify: %d pending long (taking batch of %d, %d short done)", len(long_items), len(batch), len(short))
        classified = await _classify_store(batch, "Background classify")
        log.info("Background classify done: %d/%d classified in batch", classified, len(batch))
        return  # leave backfill for a run when the live queue is clear

    # Live queue empty + quota alive → backfill already-sent items still empty
    # (e.g. bootstrap residue frozen before classification). Small batch only.
    backlog = await get_sent_empty_items(limit)
    if not backlog:
        log.debug("Background classify: no pending items, nothing to backfill")
        return
    bshort, blong = _split(backlog)
    for item, raw in bshort:
        await update_item_classification(item["id"], raw, "")
    if blong:
        filled = await _classify_store(blong, "Background backfill")
        log.info("Background backfill: %d/%d sent-empty items re-classified (%d short)", filled, len(blong), len(bshort))
    elif bshort:
        log.info("Background backfill: %d short sent-empty items filled", len(bshort))


_FILTER_SYSTEM_PROMPT = """You are a content filter. Given news items (each tagged [source/category]) and rules describing junk to exclude, decide which items to block.

Rate each potential match with a confidence score 1-10:
- 9-10: unmistakably matches the rule
- 7-8: clearly matches, minor doubt
- 5-6: borderline — lean toward keeping
- 1-4: does not match, keep

WHAT IS NEVER JUNK (do not block regardless of rule wording):
- Reporting by a news outlet on company deals, earnings, market moves, product launches, or industry plans — this is journalism, not advertising, even if it mentions prices or brand names.
- War/conflict news with concrete outcomes: destroyed equipment, strikes with confirmed results, territorial changes — this is hard news, not a "short real-time signal."
- Analysis, commentary, or op-eds from known media sources — not "collections of recommendations."
- Any item that is short or ambiguous: default confidence ≤ 5 (keep).

Output JSON only: {"blocked": [{"id": <int>, "rule": <rule_index_0based>, "confidence": <int 1-10>}]}
If nothing should be blocked: {"blocked": []}"""

_FILTER_BLOCK_THRESHOLD = 7


async def check_blocked_filters(
    items: list[dict],
    rules: list[str],
) -> dict[int, str]:
    """Check items against semantic filter rules via LLM.

    items: list of {"id": int, "text": str, "source": str, "category": str}
    rules: list of rule description strings
    Returns: {item_id: matched_rule_text} for items that should be blocked.
    Returns {} on quota exhaustion or error (pass-through, no blocking).
    """
    if not items or not rules:
        return {}

    numbered_rules = "\n".join(f"{i}. {r}" for i, r in enumerate(rules))
    _CHUNK = 25
    result: dict[int, str] = {}
    for i in range(0, len(items), _CHUNK):
        chunk = items[i:i + _CHUNK]
        numbered_items = "\n".join(
            f"{item['id']} [{item.get('source', '?')}/{item.get('category', '?')}]: {(item['text'] or '')[:150]}"
            for item in chunk
        )
        user_msg = f"Filter rules:\n{numbered_rules}\n\nItems:\n{numbered_items}"
        data = await groq_json(
            messages=[
                {"role": "system", "content": _FILTER_SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            max_retries=3,
            model=settings.groq_model_batch,
            fallback_model=settings.groq_model_fallback,
        )
        if not data or not isinstance(data.get("blocked"), list):
            continue
        for entry in data["blocked"]:
            item_id = entry.get("id")
            rule_idx = entry.get("rule")
            confidence = entry.get("confidence", 10)
            if (
                isinstance(item_id, int)
                and isinstance(rule_idx, int)
                and 0 <= rule_idx < len(rules)
                and isinstance(confidence, int)
                and confidence >= _FILTER_BLOCK_THRESHOLD
            ):
                result[item_id] = rules[rule_idx]
                log.info("Filter: blocked item id=%d | rule=%r | confidence=%d", item_id, rules[rule_idx], confidence)
            elif isinstance(item_id, int) and isinstance(rule_idx, int) and 0 <= rule_idx < len(rules):
                log.info("Filter: kept item id=%d | rule=%r | confidence=%d (below threshold %d)", item_id, rules[rule_idx], confidence, _FILTER_BLOCK_THRESHOLD)
    return result

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field

from groq import AsyncGroq, RateLimitError

from src.config import settings

log = logging.getLogger(__name__)

_client = AsyncGroq(api_key=settings.groq_api_key)

_MEDIA_PREFIX_RE = re.compile(r"^\[(?:Photo|Video|Audio|Document|Sticker|GIF|Animation)\]\s*", re.IGNORECASE)
_TRIVIAL_MAX_LEN = 60


def _strip_media_prefix(text: str) -> str:
    return _MEDIA_PREFIX_RE.sub("", text).strip()

_RATE = 25.0 / 60.0
_CAPACITY = 3.0
_tokens: float = _CAPACITY
_last_refill: float = time.monotonic()
_call_lock = asyncio.Lock()
_backoff_until: float = 0.0
_quota_dead_until: float = 0.0
_last_alert_time: float = 0.0
_ALERT_COOLDOWN = 1800.0
_QUOTA_DEAD_THRESHOLD = 300.0


def _signal_backoff(seconds: float = 65.0) -> None:
    global _backoff_until, _tokens
    _backoff_until = time.monotonic() + seconds
    _tokens = 0.0
    log.warning("Groq rate limit: signalling %gs backoff, bucket drained", seconds)


def _parse_reset(value: str | None) -> float | None:
    """Parse Groq reset header like '2m59.56s', '15s', '1h30m'."""
    if not value:
        return None
    try:
        total = 0.0
        num = ""
        for ch in value.strip():
            if ch.isdigit() or ch == ".":
                num += ch
            elif ch == "h" and num:
                total += float(num) * 3600
                num = ""
            elif ch == "m" and num:
                total += float(num) * 60
                num = ""
            elif ch == "s" and num:
                total += float(num)
                num = ""
        if num:
            total += float(num)
        return total if total > 0 else None
    except (ValueError, AttributeError):
        return None


def _extract_retry_after(exc) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) if response is not None else None
    if not headers:
        return None
    candidates = [
        headers.get("retry-after"),
        headers.get("x-ratelimit-reset-tokens"),
        headers.get("x-ratelimit-reset-requests"),
    ]
    values = [v for v in (_parse_reset(c) if c else None for c in candidates) if v is not None]
    return max(values) if values else None


def is_quota_dead() -> bool:
    return _quota_dead_until > time.monotonic()


def _signal_quota_dead(seconds: float) -> None:
    global _quota_dead_until, _backoff_until, _tokens
    _quota_dead_until = time.monotonic() + seconds
    _backoff_until = _quota_dead_until
    _tokens = 0.0
    log.warning("Groq quota exhausted: dead for %.0fs (until reset)", seconds)

_SYSTEM_PROMPT = """Summarize news for a Ukrainian digest. Output JSON only.

summary: Ukrainian. Use up to 15 words for simple news; up to 25 words when the event has multiple key details (numbers, names, consequences). Start with the key entity (person, org, asset, place). Strong verb. Keep all numbers and names exact. Do not abbreviate proper nouns. Never start with: повідомляється, стало відомо, з'явилась інформація, відбулась подія, автор, допис, пост, розповідає, пише.

key_phrase: 1-3 words, best anchor for the link. Priority: person > org > asset ticker > action phrase > location. Use a generic Ukrainian city only if nothing more distinctive exists. Never: автор, допис, інформація, подія, новина.

Example:
  IN: "Bitcoin price drops 8% after Fed rate hike"
  OUT: {"summary": "Bitcoin впав на 8% після рішення ФРС підвищити ставку", "key_phrase": "Bitcoin"}

Respond ONLY with JSON: {"summary": "...", "key_phrase": "..."}"""

_BATCH_SYSTEM_PROMPT = """Group news items from one source by event, then summarize each group in Ukrainian. Output JSON only.

Items are numbered from 0. Every item MUST appear in exactly one group — no item may be omitted or duplicated.

MERGE RULE: merge ONLY items that describe THE SAME SPECIFIC EVENT with new developments (same attack, same trial, same announcement, same person's statement on same day). Do NOT merge items that are merely about the same topic, person, or organisation if they are different events.
Examples of correct merges: "Air alert in Kyiv" + "All-clear in Kyiv" = one group. "Zelensky signed decree X" + "Details of decree X released" = one group.
Examples of wrong merges: "OPEC raises output" + "Saudi Arabia oil strategy" = separate groups. "Trump raised tariffs" + "EU responds to tariffs" = separate groups.

Per group:
- summary: Ukrainian. Single item: up to 20 words. Merged (2+ items): up to 30 words — include the key development from each merged item. Start with key entity (person, org, asset, place). Strong verb. Keep all numbers and names exact. Never start with: повідомляється, стало відомо, автор, допис, пост.
- key_phrase: 1-3 words. Priority: person > org > asset > action > location. Never: автор, допис, інформація, подія.

Respond ONLY with JSON: {"groups": [{"ids": [0], "summary": "...", "key_phrase": "..."}]}"""

_MULTI_SYSTEM_PROMPT = """Summarize each news item separately in Ukrainian. Output JSON only.

Items are numbered from 0. Produce exactly one entry per input id. Do NOT merge items.

Per item:
- summary: Ukrainian. Up to 20 words; up to 25 words for events with multiple key details. Start with the key entity (person, org, asset, place). Strong verb. Keep all numbers and names exact. Do not abbreviate proper nouns. Never start with: повідомляється, стало відомо, автор, допис, пост.
- key_phrase: 1-3 words. Priority: person > org > asset > action > location. Generic city only if nothing better. Never: автор, допис, інформація, подія.

Respond ONLY with JSON: {"items": [{"id": 0, "summary": "...", "key_phrase": "..."}]}"""


@dataclass
class ClassificationResult:
    summary: str
    key_phrase: str = field(default="")


def _refill_tokens() -> None:
    global _tokens, _last_refill
    now = time.monotonic()
    _tokens = min(_CAPACITY, _tokens + (now - _last_refill) * _RATE)
    _last_refill = now


async def _groq_call(messages: list[dict], max_retries: int) -> dict:
    global _tokens
    if is_quota_dead():
        log.debug("Groq quota dead, short-circuiting call (%.0fs remaining)", _quota_dead_until - time.monotonic())
        return {}
    for attempt in range(max_retries):
        async with _call_lock:
            now = time.monotonic()
            if _backoff_until > now:
                await asyncio.sleep(_backoff_until - now)
            _refill_tokens()
            if _tokens < 1.0:
                wait = (1.0 - _tokens) / _RATE
                await asyncio.sleep(wait)
                _refill_tokens()
            _tokens -= 1.0
            try:
                response = await _client.chat.completions.create(
                    model=settings.groq_model,
                    messages=messages,
                    response_format={"type": "json_object"},
                    temperature=0.1,
                )
                return json.loads(response.choices[0].message.content)
            except RateLimitError as exc:
                retry_after = _extract_retry_after(exc)
                if retry_after is not None and retry_after >= _QUOTA_DEAD_THRESHOLD:
                    _signal_quota_dead(retry_after)
                    await _maybe_send_rate_limit_alert()
                    return {}
                _signal_backoff(retry_after if retry_after else 65.0)
                if attempt < max_retries - 1:
                    log.warning("Groq rate limit hit, retrying after backoff (attempt %d/%d)", attempt + 1, max_retries)
                else:
                    log.warning("Groq rate limit persistent after %d attempts, using fallback", max_retries)
                    await _maybe_send_rate_limit_alert()
            except Exception as exc:
                log.warning("Groq call error: %s", exc)
                return {}
    return {}


async def classify(text: str, prompt_extra: str | None = None, max_retries: int = 5) -> ClassificationResult:
    stripped = _strip_media_prefix(text)
    if len(stripped) < _TRIVIAL_MAX_LEN:
        log.debug("classify: trivial text (%d chars after strip), using raw as summary", len(stripped))
        return ClassificationResult(summary=text.strip(), key_phrase="")

    system = _SYSTEM_PROMPT
    if prompt_extra:
        system = f"{_SYSTEM_PROMPT}\n\nAdditional instructions: {prompt_extra}"

    data = await _groq_call(
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": text[:1500]},
        ],
        max_retries=max_retries,
    )
    result = ClassificationResult(
        summary=data.get("summary", ""),
        key_phrase=data.get("key_phrase", ""),
    )
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
    numbered = "\n".join(f"{item['id']}: {item['text'][:700]}" for item in items)
    data = await _groq_call(
        messages=[
            {"role": "system", "content": _MULTI_SYSTEM_PROMPT},
            {"role": "user", "content": numbered},
        ],
        max_retries=3,
    )
    out: dict[int, ClassificationResult] = {}
    for row in data.get("items", []):
        try:
            rid = int(row["id"])
        except (KeyError, TypeError, ValueError):
            continue
        out[rid] = ClassificationResult(
            summary=row.get("summary", "") or "",
            key_phrase=row.get("key_phrase", "") or "",
        )
    log.debug("Batch classified %d/%d items", len(out), len(items))
    return out


_NO_MERGE_KEYWORDS = ("no merge", "no_merge", "не мерджити", "не об'єднувати", "не объединять", "окремо", "separate")


def _wants_no_merge(prompt_extra: str | None) -> bool:
    if not prompt_extra:
        return False
    lower = prompt_extra.lower()
    return any(kw in lower for kw in _NO_MERGE_KEYWORDS)


async def group_by_topic(items: list[dict], prompt_extra: str | None = None) -> list[dict]:
    """
    items: list of {"id": int, "text": str}
    Returns: list of {"ids": [int, ...], "summary": str, "key_phrase": str}
    Falls back to one group per item on error.
    """
    all_ids = {item["id"] for item in items}

    if _wants_no_merge(prompt_extra):
        log.info("group_by_topic: no-merge instruction in prompt_extra, using multi-summarise")
        numbered = "\n".join(f"{item['id']}: {item['text'][:700]}" for item in items)
        system = _MULTI_SYSTEM_PROMPT
        if prompt_extra:
            system = f"{_MULTI_SYSTEM_PROMPT}\n\nAdditional instructions: {prompt_extra}"
        data = await _groq_call(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": numbered},
            ],
            max_retries=3,
        )
        result = []
        covered: set[int] = set()
        for row in data.get("items", []):
            try:
                rid = int(row["id"])
            except (KeyError, TypeError, ValueError):
                continue
            covered.add(rid)
            result.append({
                "ids": [rid],
                "summary": row.get("summary", "") or "",
                "key_phrase": row.get("key_phrase", "") or "",
            })
        for mid in all_ids - covered:
            result.append({"ids": [mid], "summary": "", "key_phrase": ""})
        log.debug("No-merge summarised %d items into %d entries", len(items), len(result))
        return result

    numbered = "\n".join(f"{item['id']}: {item['text'][:700]}" for item in items)
    system = _BATCH_SYSTEM_PROMPT
    if prompt_extra and not _wants_no_merge(prompt_extra):
        system = f"{_BATCH_SYSTEM_PROMPT}\n\nAdditional instructions: {prompt_extra}"

    data = await _groq_call(
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": numbered},
        ],
        max_retries=3,
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
        result.append({
            "ids": ids,
            "summary": g.get("summary", ""),
            "key_phrase": g.get("key_phrase", ""),
        })

    missing = all_ids - covered_ids
    if missing:
        log.warning("group_by_topic: %d item(s) missing from AI output, adding as singletons: %s", len(missing), missing)
        for mid in missing:
            result.append({"ids": [mid], "summary": "", "key_phrase": ""})

    log.debug("Grouped %d items into %d groups", len(items), len(result))
    return result


async def _maybe_send_rate_limit_alert() -> None:
    global _last_alert_time
    now = time.monotonic()
    if now - _last_alert_time < _ALERT_COOLDOWN:
        return
    _last_alert_time = now
    try:
        from src.dispatcher.sender import send_alert
        await send_alert("Groq rate limit exhausted — summaries will fall back to raw text until quota resets")
    except Exception:
        pass


async def classify_pending_items(limit: int = 3) -> None:
    from src.db.models import get_unsent_items, update_item_classification
    items = await get_unsent_items()
    pending = [
        item for item in items
        if not (item["summary"] or "").strip() and (item["raw_text"] or "").strip()
    ]
    if not pending:
        log.debug("Background classify: no pending items with empty summary")
        return

    short, long_items = [], []
    for item in pending:
        raw = (item["raw_text"] or "").strip()
        if len(_strip_media_prefix(raw)) < _TRIVIAL_MAX_LEN:
            short.append((item, raw))
        else:
            long_items.append((item, raw))

    for item, raw in short:
        await update_item_classification(item["id"], raw, "")
        log.info("Background classify: short text used as summary for item id=%d", item["id"])

    if not long_items:
        log.info("Background classify done: %d short / 0 long", len(short))
        return
    if is_quota_dead():
        log.info("Background classify: quota dead, skipping %d long items", len(long_items))
        return

    batch = long_items[:limit]
    log.info("Background classify: %d pending (taking batch of %d, %d short done)", len(long_items), len(batch), len(short))
    batch_input = [{"id": item["id"], "text": raw} for item, raw in batch]
    results = await classify_batch(batch_input)
    classified = 0
    for item, _raw in batch:
        result = results.get(item["id"])
        if result and result.summary:
            await update_item_classification(item["id"], result.summary, result.key_phrase)
            log.info("Background classify: item id=%d | summary=%s", item["id"], result.summary)
            classified += 1
        else:
            log.debug("Background classify: no result for item id=%d (will retry next pass)", item["id"])
    log.info("Background classify done: %d/%d classified in batch", classified, len(batch))

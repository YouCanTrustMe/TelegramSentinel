import asyncio
import json
import logging
import time
from dataclasses import dataclass, field

from groq import AsyncGroq, RateLimitError

from src.config import settings

log = logging.getLogger(__name__)

_client = AsyncGroq(api_key=settings.groq_api_key)

_RATE = 25.0 / 60.0
_CAPACITY = 3.0
_tokens: float = _CAPACITY
_last_refill: float = time.monotonic()
_call_lock = asyncio.Lock()
_backoff_until: float = 0.0
_last_alert_time: float = 0.0
_ALERT_COOLDOWN = 1800.0


def _signal_backoff(seconds: float = 65.0) -> None:
    global _backoff_until, _tokens
    _backoff_until = time.monotonic() + seconds
    _tokens = 0.0
    log.warning("Groq rate limit: signalling %gs backoff, bucket drained", seconds)

_SYSTEM_PROMPT = """You are a news summarizer for a Ukrainian-language digest.

Your task:
1. Write a summary in Ukrainian (translate if not Ukrainian), up to 15 words.
   - Start with the key entity: a specific person, place, organization, or asset.
   - State the concrete action or event with a strong verb.
   - Include specific numbers, names, or locations where present.
   - NEVER start with: "повідомляється", "з'явилась інформація", "стало відомо", "відбулась подія",
     "автор", "допис", "пост", "розповідає", "пише".
   - Never abbreviate proper nouns.

2. Extract "key_phrase": 1-3 words — the best anchor text for the news link.
   - Priority: person name > org name > asset ticker > action phrase > location.
   - Use a location ONLY if it is the sole distinctive element (e.g. a foreign country, a specific battlefield). Never use a generic Ukrainian city as key_phrase if a person, org, or action is available.
   - If no proper noun: use the most distinctive verb phrase from the summary (the main action).
   - NEVER use: "автор", "допис", "повідомляється", "інформація", "подія", "новина".

Examples:
  INPUT: "Автор анонсував новий канал про крипту і бізнес"
  BAD summary: "Автор створює новий канал для обговорення крипти та бізнесу"
  BAD key_phrase: "автор"
  GOOD summary: "Капітаник запускає новий канал про крипту та бізнес"
  GOOD key_phrase: "Капітаник"

  INPUT: (market news about Bitcoin) "Bitcoin price drops 8% after Fed rate hike decision"
  BAD summary: "З'явилась інформація про криптовалютний ринок"
  BAD key_phrase: "інформація"
  GOOD summary: "Bitcoin впав на 8% після рішення ФРС підвищити ставку"
  GOOD key_phrase: "Bitcoin"

  INPUT: (NBU raises rate) "Національний банк підвищив облікову ставку до 15,5%"
  BAD summary: "Повідомляється про важливу подію у фінансовому секторі"
  GOOD summary: "НБУ підвищив облікову ставку до 15,5% через інфляційний тиск"
  GOOD key_phrase: "НБУ"

  INPUT: (court verdict) "Суд засудив заступника голови Рівненської облради до 9 років"
  BAD summary: "Стало відомо про судове рішення щодо чиновника"
  GOOD summary: "Заступника голови Рівненської облради засуджено до 9 років"
  GOOD key_phrase: "Рівненська облрада"

  INPUT: (crypto theft news) "California man sentenced 78 months for $250M crypto theft conspiracy"
  BAD summary: "Американця засуджено за крадіжку криптовалюти"
  GOOD summary: "Марлон Ферро вламувався до будинків, щоб викрасти гаманці на $250M"
  GOOD key_phrase: "Марлон Ферро"

  INPUT: (vague trade post) "Торгую на біржі, ось мої угоди сьогодні"
  BAD summary: "Допис про торговельну діяльність"
  GOOD summary: "Автор показує свої торгові угоди на біржі"
  GOOD key_phrase: "торгові угоди"

  INPUT: "Чоловік стріляв у Черкасах, бо сусід зірвав бузок"
  BAD key_phrase: "Черкаси"  (generic city — not the distinctive element)
  GOOD key_phrase: "стріляв через бузок"  (the distinctive action)

Respond ONLY with valid JSON:
{"summary": "<Ukrainian, up to 15 words>", "key_phrase": "<1-3 words>"}"""

_BATCH_SYSTEM_PROMPT = """You are a news summarizer for a Ukrainian-language digest.

You will receive multiple news items from the same source, numbered starting from 0.
Your tasks:
1. Group items that cover the same event or topic (follow-ups and updates count as same topic).

2. For each group write ONE summary in Ukrainian (translate if not Ukrainian):
   - Single item: up to 15 words. Multiple items merged: up to 30 words combining key facts.
   - Start with the key entity: a specific person, place, organization, or asset.
   - State the concrete action or event with a strong verb.
   - Include specific numbers, names, or locations where present.
   - NEVER start with: "повідомляється", "з'явилась інформація", "стало відомо", "відбулась подія",
     "автор", "допис", "пост", "розповідає", "пише".
   - Never abbreviate proper nouns.

3. Extract "key_phrase": 1-3 words — the best anchor text for the news link.
   - Priority: person name > org name > asset ticker > action phrase > location.
   - Use a location ONLY if it is the sole distinctive element. Never use a generic Ukrainian city if a person, org, or action is available.
   - NEVER use: "автор", "допис", "повідомляється", "інформація", "подія".

4. Never abbreviate proper nouns (person names, place names, organizations, brands).

Every item must appear in exactly one group.

Respond ONLY with valid JSON:
{"groups": [{"ids": [0], "summary": "Коротке резюме", "key_phrase": "Ключове слово"}, {"ids": [1, 2], "summary": "Об'єднане резюме", "key_phrase": "Ключове слово"}]}"""


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
            except RateLimitError:
                _signal_backoff()
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
    system = _SYSTEM_PROMPT
    if prompt_extra:
        system = f"{_SYSTEM_PROMPT}\n\nAdditional instructions: {prompt_extra}"

    data = await _groq_call(
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": text[:4000]},
        ],
        max_retries=max_retries,
    )
    result = ClassificationResult(
        summary=data.get("summary", ""),
        key_phrase=data.get("key_phrase", ""),
    )
    log.debug("Classified: %s | key=%s", result.summary, result.key_phrase)
    return result


async def group_by_topic(items: list[dict]) -> list[dict]:
    """
    items: list of {"id": int, "text": str}
    Returns: list of {"ids": [int, ...], "summary": str, "key_phrase": str}
    Falls back to one group per item on error.
    """
    numbered = "\n".join(f"{item['id']}: {item['text'][:600]}" for item in items)

    data = await _groq_call(
        messages=[
            {"role": "system", "content": _BATCH_SYSTEM_PROMPT},
            {"role": "user", "content": numbered},
        ],
        max_retries=3,
    )
    groups = data.get("groups", [])
    if not groups:
        log.warning("Batch grouping returned empty, falling back to individual items")
        return [{"ids": [item["id"]], "summary": "", "key_phrase": ""} for item in items]

    result = []
    for g in groups:
        result.append({
            "ids": [int(i) for i in g["ids"]],
            "summary": g.get("summary", ""),
            "key_phrase": g.get("key_phrase", ""),
        })
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


async def classify_pending_items() -> None:
    from src.db.models import get_unsent_items, update_item_classification
    items = await get_unsent_items()
    pending = [
        item for item in items
        if not (item["summary"] or "").strip() and (item["raw_text"] or "").strip()
    ]
    if not pending:
        log.debug("Background classify: no pending items with empty summary")
        return
    log.info("Background classify: %d unsent items with empty summary", len(pending))
    classified = 0
    for item in pending:
        raw = (item["raw_text"] or "").strip()
        if len(raw) < 15:
            await update_item_classification(item["id"], raw, "")
            log.info("Background classify: short text used as summary for item id=%d", item["id"])
            classified += 1
        else:
            result = await classify(raw)
            if result.summary:
                await update_item_classification(item["id"], result.summary, result.key_phrase)
                log.info("Background classify: item id=%d | summary=%s", item["id"], result.summary)
                classified += 1
    log.info("Background classify done: %d/%d classified", classified, len(pending))

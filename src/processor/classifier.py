import asyncio
import json
import logging
import time
from dataclasses import dataclass, field

from groq import AsyncGroq, RateLimitError

from src.config import settings

log = logging.getLogger(__name__)

_client = AsyncGroq(api_key=settings.groq_api_key)

_rate_lock = asyncio.Lock()
_last_call_time: float = 0.0
_backoff_until: float = 0.0
_MIN_INTERVAL = 60.0 / 25  # 25 RPM — buffer below 30 RPM free tier limit


def _signal_backoff(seconds: float = 65.0) -> None:
    """Tell the whole queue to pause: all waiting classify() calls will hold off."""
    global _backoff_until
    _backoff_until = time.monotonic() + seconds
    log.warning("Groq rate limit: signalling %gs backoff to all queued calls", seconds)

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


async def _acquire_rate_slot() -> None:
    global _last_call_time
    async with _rate_lock:
        now = time.monotonic()
        wait_until = max(_last_call_time + _MIN_INTERVAL, _backoff_until)
        if wait_until > now:
            await asyncio.sleep(wait_until - now)
        _last_call_time = time.monotonic()


async def classify(text: str, prompt_extra: str | None = None) -> ClassificationResult:
    system = _SYSTEM_PROMPT
    if prompt_extra:
        system = f"{_SYSTEM_PROMPT}\n\nAdditional instructions: {prompt_extra}"

    for attempt in range(2):
        await _acquire_rate_slot()
        try:
            response = await _client.chat.completions.create(
                model=settings.groq_model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": text[:4000]},
                ],
                response_format={"type": "json_object"},
                temperature=0.1,
            )
            data = json.loads(response.choices[0].message.content)
            result = ClassificationResult(
                summary=data.get("summary", ""),
                key_phrase=data.get("key_phrase", ""),
            )
            log.debug("Classified: %s | key=%s", result.summary, result.key_phrase)
            return result
        except RateLimitError:
            _signal_backoff()
            if attempt == 0:
                log.warning("Groq rate limit hit, retrying via queue after backoff")
            else:
                log.warning("Groq rate limit persistent, using fallback")
        except Exception as exc:
            log.warning("Classification error, using fallback: %s", exc)
            break

    return ClassificationResult(summary="", key_phrase="")


async def group_by_topic(items: list[dict]) -> list[dict]:
    """
    items: list of {"id": int, "text": str}
    Returns: list of {"ids": [int, ...], "score": int, "summary": str, "key_phrase": str}
    Falls back to one group per item on error.
    """
    numbered = "\n".join(f"{item['id']}: {item['text'][:600]}" for item in items)

    for attempt in range(2):
        await _acquire_rate_slot()
        try:
            response = await _client.chat.completions.create(
                model=settings.groq_model,
                messages=[
                    {"role": "system", "content": _BATCH_SYSTEM_PROMPT},
                    {"role": "user", "content": numbered},
                ],
                response_format={"type": "json_object"},
                temperature=0.1,
            )
            data = json.loads(response.choices[0].message.content)
            groups = data.get("groups", [])
            result = []
            for g in groups:
                result.append({
                    "ids": [int(i) for i in g["ids"]],
                    "summary": g.get("summary", ""),
                    "key_phrase": g.get("key_phrase", ""),
                })
            log.debug("Grouped %d items into %d groups", len(items), len(result))
            return result
        except RateLimitError:
            _signal_backoff()
            if attempt == 0:
                log.warning("Groq rate limit hit during batch grouping, retrying via queue after backoff")
            else:
                log.warning("Groq rate limit persistent during batch grouping, falling back")
        except Exception as exc:
            log.warning("Batch grouping error, falling back to individual items: %s", exc)
            break

    return [{"ids": [item["id"]], "summary": "", "key_phrase": ""} for item in items]

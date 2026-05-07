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
_MIN_INTERVAL = 60.0 / 29  # stay just under 30 RPM free tier

_SYSTEM_PROMPT = """You are a news summarizer for a Ukrainian-language digest.

Your task:
1. Rate the news importance 1-5:
   5 = breaking news, attacks, casualties, official decisions, arrests, disasters
   4 = significant developments, confirmed events, policy changes
   3 = regular updates, ongoing situations, market moves
   2 = analysis, opinions, forecasts, soft news
   1 = ads, self-promotion, reposts, entertainment, polls

2. Write a summary in Ukrainian (translate if not Ukrainian), up to 15 words.
   - Start with the key entity: a specific person, place, organization, or asset.
   - State the concrete action or event with a strong verb.
   - Include specific numbers, names, or locations where present.
   - NEVER start with: "повідомляється", "з'явилась інформація", "стало відомо", "відбулась подія",
     "автор", "допис", "пост", "розповідає", "пише".
   - Never abbreviate proper nouns.

3. Extract "key_phrase": 1-3 words — the most specific identifier of the subject.
   - Prefer a proper noun: person name, org name, asset ticker, location.
   - If no clear proper noun: use the most distinctive verb or noun from the summary.
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

Respond ONLY with valid JSON:
{"score": 1-5, "summary": "<Ukrainian, up to 15 words>", "key_phrase": "<1-3 words>"}"""

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

3. Extract "key_phrase": 1-3 words — the most specific identifier of the subject for the group.
   - Prefer a proper noun: person name, org name, asset ticker, location.
   - NEVER use: "автор", "допис", "повідомляється", "інформація", "подія".

4. Rate group importance 1-5:
   5 = breaking news, attacks, casualties, official decisions, arrests, disasters
   4 = significant developments, confirmed events, policy changes
   3 = regular updates, ongoing situations, market moves
   2 = analysis, opinions, forecasts, soft news
   1 = ads, self-promotion, reposts, entertainment, polls

5. Never abbreviate proper nouns (person names, place names, organizations, brands).
6. Stars: 5=★★★★★ 4=★★★★☆ 3=★★★☆☆ 2=★★☆☆☆ 1=★☆☆☆☆

Every item must appear in exactly one group.

Respond ONLY with valid JSON:
{"groups": [{"ids": [0], "score": 3, "summary": "Коротке резюме", "key_phrase": "Ключове слово"}, {"ids": [1, 2], "score": 4, "summary": "Об'єднане резюме", "key_phrase": "Ключове слово"}]}"""


@dataclass
class ClassificationResult:
    summary: str
    key_phrase: str = field(default="")


async def _acquire_rate_slot() -> None:
    global _last_call_time
    async with _rate_lock:
        elapsed = time.monotonic() - _last_call_time
        if elapsed < _MIN_INTERVAL:
            await asyncio.sleep(_MIN_INTERVAL - elapsed)
        _last_call_time = time.monotonic()


async def classify(text: str, prompt_extra: str | None = None) -> ClassificationResult:
    await _acquire_rate_slot()

    system = _SYSTEM_PROMPT
    if prompt_extra:
        system = f"{_SYSTEM_PROMPT}\n\nAdditional instructions: {prompt_extra}"

    for attempt in range(2):
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
            score = max(1, min(5, int(data.get("score", 1))))
            stars = "★" * score + "☆" * (5 - score)
            result = ClassificationResult(
                summary=f"{stars} {data.get('summary', '')}",
                key_phrase=data.get("key_phrase", ""),
            )
            log.debug("Classified: %s | key=%s", result.summary, result.key_phrase)
            return result
        except RateLimitError:
            if attempt == 0:
                log.warning("Groq rate limit hit, waiting 30s")
                await asyncio.sleep(30)
            else:
                log.warning("Groq rate limit hit again, using fallback")
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
    await _acquire_rate_slot()

    numbered = "\n".join(f"{item['id']}: {item['text'][:600]}" for item in items)

    for attempt in range(2):
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
                score = max(1, min(5, int(g.get("score", 3))))
                stars = "★" * score + "☆" * (5 - score)
                result.append({
                    "ids": [int(i) for i in g["ids"]],
                    "score": score,
                    "summary": f"{stars} {g.get('summary', '')}",
                    "key_phrase": g.get("key_phrase", ""),
                })
            log.debug("Grouped %d items into %d groups", len(items), len(result))
            return result
        except RateLimitError:
            if attempt == 0:
                log.warning("Groq rate limit hit during batch grouping, waiting 30s")
                await asyncio.sleep(30)
            else:
                log.warning("Groq rate limit hit again during batch grouping, falling back")
        except Exception as exc:
            log.warning("Batch grouping error, falling back to individual items: %s", exc)
            break

    return [{"ids": [item["id"]], "score": 3, "summary": "", "key_phrase": ""} for item in items]

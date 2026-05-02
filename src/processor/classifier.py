import asyncio
import json
import logging
import time
from dataclasses import dataclass

from groq import AsyncGroq, RateLimitError

from src.config import settings

log = logging.getLogger(__name__)

_client = AsyncGroq(api_key=settings.groq_api_key)

_rate_lock = asyncio.Lock()
_last_call_time: float = 0.0
_MIN_INTERVAL = 60.0 / 25  # stay under 30 RPM free tier

_SYSTEM_PROMPT = """You are a news summarizer.

Your task:
1. Rate the news 1-5: 5=breaking/facts/decisions, 1=ads/opinions/reposts.
2. Write a summary in Ukrainian, max 7-8 words. Use your own words if needed.

Respond ONLY with valid JSON:
{"score": 1-5, "summary": "<Ukrainian max 8 words>"}"""


@dataclass
class ClassificationResult:
    summary: str


async def classify(text: str) -> ClassificationResult:
    global _last_call_time
    async with _rate_lock:
        elapsed = time.monotonic() - _last_call_time
        if elapsed < _MIN_INTERVAL:
            await asyncio.sleep(_MIN_INTERVAL - elapsed)
        _last_call_time = time.monotonic()

    for attempt in range(2):
        try:
            response = await _client.chat.completions.create(
                model=settings.groq_model,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": text[:4000]},
                ],
                response_format={"type": "json_object"},
                temperature=0.1,
            )
            data = json.loads(response.choices[0].message.content)
            score = max(1, min(5, int(data.get("score", 1))))
            stars = "★" * score + "☆" * (5 - score)
            result = ClassificationResult(summary=f"{stars} {data.get('summary', '')}")
            log.debug("Classified: %s", result.summary)
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

    return ClassificationResult(summary="")

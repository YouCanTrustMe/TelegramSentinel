import asyncio
import json
import logging
import time
from dataclasses import dataclass

import google.generativeai as genai
from google.api_core.exceptions import ResourceExhausted

from src.config import settings

log = logging.getLogger(__name__)

genai.configure(api_key=settings.gemini_api_key)

_rate_lock = asyncio.Lock()
_last_call_time: float = 0.0
_MIN_INTERVAL = 60.0 / 14  # stay under 15 RPM free tier

_SYSTEM_PROMPT = """You are a news summarizer.

Your task:
1. Rate the news 1-5: 5=breaking/facts/decisions, 1=ads/opinions/reposts.
2. Write a summary in Ukrainian, max 7-8 words. Use your own words if needed.

Respond ONLY with valid JSON:
{"score": 1-5, "summary": "<Ukrainian max 8 words>"}"""

_model = genai.GenerativeModel(
    model_name=settings.gemini_model,
    system_instruction=_SYSTEM_PROMPT,
)


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

    for attempt in range(3):
        try:
            response = await _model.generate_content_async(
                text[:4000],
                generation_config=genai.GenerationConfig(
                    response_mime_type="application/json",
                    temperature=0.1,
                ),
            )
            data = json.loads(response.text)
            score = max(1, min(5, int(data.get("score", 1))))
            stars = "★" * score + "☆" * (5 - score)
            result = ClassificationResult(summary=f"{stars} {data.get('summary', '')}")
            log.debug("Classified: %s", result.summary)
            return result
        except ResourceExhausted:
            wait = 180
            log.warning("Gemini quota hit, waiting %ds (attempt %d/3)", wait, attempt + 1)
            await asyncio.sleep(wait)
        except Exception as exc:
            log.warning("Classification error, using fallback: %s", exc)
            return ClassificationResult(summary="")

    log.warning("Classification failed after 3 retries, using fallback")
    return ClassificationResult(summary="")

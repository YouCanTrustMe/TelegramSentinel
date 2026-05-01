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

_SYSTEM_PROMPT = """You are a news importance classifier and summarizer.

Your task:
1. Classify the news as "high" or "low" importance.
2. Write a one-sentence summary in Ukrainian (max 150 characters).

High importance criteria:
- Contains specific facts, numbers, decisions, or events
- Not an opinion, advertisement, or repost without new information

Respond ONLY with valid JSON:
{"importance": "high" | "low", "summary": "<Ukrainian summary>"}"""

_model = genai.GenerativeModel(
    model_name=settings.gemini_model,
    system_instruction=_SYSTEM_PROMPT,
)


@dataclass
class ClassificationResult:
    importance: str
    summary: str


async def classify(text: str) -> ClassificationResult:
    global _last_call_time
    async with _rate_lock:
        elapsed = time.monotonic() - _last_call_time
        if elapsed < _MIN_INTERVAL:
            await asyncio.sleep(_MIN_INTERVAL - elapsed)
        _last_call_time = time.monotonic()

    for attempt in range(4):
        try:
            response = await _model.generate_content_async(
                text[:4000],
                generation_config=genai.GenerationConfig(
                    response_mime_type="application/json",
                    temperature=0.1,
                ),
            )
            data = json.loads(response.text)
            result = ClassificationResult(
                importance=data.get("importance", "low"),
                summary=data.get("summary", ""),
            )
            log.debug("Classified: importance=%s", result.importance)
            return result
        except ResourceExhausted:
            wait = 5 * 2 ** attempt  # 5s, 10s, 20s, 40s
            log.warning("Gemini rate limited, retrying in %ds (attempt %d/4)", wait, attempt + 1)
            await asyncio.sleep(wait)
        except Exception as exc:
            log.warning("Classification failed, using fallback: %s", exc)
            return ClassificationResult(importance="low", summary="")

    log.warning("Classification failed after 4 retries, using fallback")
    return ClassificationResult(importance="low", summary="")

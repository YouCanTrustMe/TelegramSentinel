import json
import logging
from dataclasses import dataclass

import google.generativeai as genai

from src.config import settings

log = logging.getLogger(__name__)

genai.configure(api_key=settings.gemini_api_key)

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
    except Exception as exc:
        log.warning("Classification failed, using fallback: %s", exc)
        return ClassificationResult(importance="low", summary="")

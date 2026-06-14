"""Embedding transport for cross-source deduplication. Talks to the Gemini
embedding API over REST (own aiohttp session, separate from the Telegram Bot
API session in dispatcher/sender.py). Fail-open by design: any error, missing
key or quota issue yields None embeddings so the caller skips dedup rather than
losing items. Kept separate from clustering logic in cross_dedup.py."""
import logging
from array import array

import aiohttp
import numpy as np

from src.config import settings

log = logging.getLogger(__name__)

_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:batchEmbedContents"
_BATCH_LIMIT = 100  # Gemini caps batchEmbedContents requests per call
_OUTPUT_DIMS = 768
_TIMEOUT = aiohttp.ClientTimeout(total=60)

_session: aiohttp.ClientSession | None = None


def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(timeout=_TIMEOUT)
    return _session


async def close_session() -> None:
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
        _session = None


def to_blob(vec: list[float] | np.ndarray) -> bytes:
    """Serialize a float vector to compact float32 bytes for the items.embedding BLOB."""
    return array("f", (float(x) for x in vec)).tobytes()


def from_blob(blob: bytes | None) -> np.ndarray | None:
    if not blob:
        return None
    return np.frombuffer(blob, dtype=np.float32)


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity of two vectors. gemini-embedding-001 at reduced
    dimensions is NOT pre-normalized, so normalize here."""
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


async def _embed_chunk(texts: list[str]) -> list[list[float] | None]:
    model = settings.gemini_embed_model
    url = _ENDPOINT.format(model=model)
    payload = {
        "requests": [
            {
                "model": f"models/{model}",
                "content": {"parts": [{"text": text or ""}]},
                "taskType": "SEMANTIC_SIMILARITY",
                "outputDimensionality": _OUTPUT_DIMS,
            }
            for text in texts
        ]
    }
    async with _get_session().post(url, params={"key": settings.gemini_api_key}, json=payload) as resp:
        if resp.status != 200:
            body = await resp.text()
            log.warning("Gemini embed failed: %s %s", resp.status, body[:300])
            return [None] * len(texts)
        data = await resp.json()
    embeddings = data.get("embeddings") or []
    if len(embeddings) != len(texts):
        log.warning("Gemini embed returned %d vectors for %d texts, skipping batch", len(embeddings), len(texts))
        return [None] * len(texts)
    return [(e.get("values") if isinstance(e, dict) else None) for e in embeddings]


async def embed_texts(texts: list[str]) -> list[list[float] | None]:
    """Embed texts in order, chunked under the API batch limit. Returns a
    list aligned with the input; any failed/missing entry is None. Never raises:
    on a missing key or transport error every entry comes back None so dedup is
    skipped, not the items."""
    if not texts:
        return []
    if not settings.gemini_api_key:
        log.warning("Gemini API key not set, skipping embeddings for %d text(s)", len(texts))
        return [None] * len(texts)

    out: list[list[float] | None] = []
    for i in range(0, len(texts), _BATCH_LIMIT):
        chunk = texts[i:i + _BATCH_LIMIT]
        try:
            out.extend(await _embed_chunk(chunk))
        except Exception as exc:
            log.warning("Gemini embed error on chunk %d: %s", i // _BATCH_LIMIT, exc)
            out.extend([None] * len(chunk))
    ok = sum(1 for v in out if v is not None)
    log.info("Embedded %d/%d text(s) via %s", ok, len(texts), settings.gemini_embed_model)
    return out

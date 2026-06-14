"""Embedding transport for cross-source deduplication. Talks to the Gemini
embedding API over REST (own aiohttp session, separate from the Telegram Bot
API session in dispatcher/sender.py). Fail-open by design: any error, missing
key or quota issue yields None embeddings so the caller skips dedup rather than
losing items. Kept separate from clustering logic in cross_dedup.py."""
import asyncio
import logging
import time
from array import array

import aiohttp
import numpy as np

from src.config import settings

log = logging.getLogger(__name__)

_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:batchEmbedContents"
# Keep a chunk at/below the per-minute budget so one chunk never alone exceeds it.
_BATCH_LIMIT = 90
_OUTPUT_DIMS = 768
_TIMEOUT = aiohttp.ClientTimeout(total=60)
_MAX_RATE_WAIT = 65.0  # cap a single throttle wait; beyond this, let it fail-open

_session: aiohttp.ClientSession | None = None

# Token-bucket rate limiter (a small async queue with backpressure) so embedding
# bursts stay under the Gemini free per-minute budget instead of hitting 429s.
_rl_tokens: float = 0.0
_rl_last: float = 0.0
_rl_lock = asyncio.Lock()


async def _rate_acquire(n: int) -> None:
    global _rl_tokens, _rl_last
    rpm = max(1, settings.embed_rpm)
    refill = rpm / 60.0
    n = min(n, rpm)
    async with _rl_lock:
        if _rl_last == 0.0:
            _rl_tokens, _rl_last = float(rpm), time.monotonic()
        while True:
            now = time.monotonic()
            _rl_tokens = min(float(rpm), _rl_tokens + (now - _rl_last) * refill)
            _rl_last = now
            if _rl_tokens >= n:
                _rl_tokens -= n
                return
            wait = min((n - _rl_tokens) / refill, _MAX_RATE_WAIT)
            log.info("Embedding rate limit: waiting %.0fs for capacity (%d texts)", wait, n)
            await asyncio.sleep(wait)


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
            await _rate_acquire(len(chunk))
            out.extend(await _embed_chunk(chunk))
        except Exception as exc:
            log.warning("Gemini embed error on chunk %d: %s", i // _BATCH_LIMIT, exc)
            out.extend([None] * len(chunk))
    ok = sum(1 for v in out if v is not None)
    log.info("Embedded %d/%d text(s) via %s", ok, len(texts), settings.gemini_embed_model)
    return out

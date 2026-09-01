"""Embedding transport for cross-source deduplication. Talks to whichever provider
`settings.embed_provider` names (own aiohttp session, separate from the Telegram Bot
API session in dispatcher/sender.py). Fail-open by design: any error, missing key or
quota issue yields None embeddings so the caller skips dedup rather than losing items.
Kept separate from clustering logic in cross_dedup.py."""
import asyncio
import logging
import re
import time
from array import array

import aiohttp
import numpy as np

from src.config import settings

log = logging.getLogger(__name__)

_GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:batchEmbedContents"
_MISTRAL_ENDPOINT = "https://api.mistral.ai/v1/embeddings"
# Vectors from two models are not comparable, and their lengths differ, so the
# dimension doubles as a stored vector's model tag: a blob of the wrong length is
# read back as "no vector" and the item is simply re-embedded with the active model.
# That is what makes switching providers safe without a migration.
_MODEL_DIMS = {"mistral-embed": 1024, "codestral-embed": 1536}
# The table is only a starting guess: EMBED_MODEL is configurable, so a model that
# is not in it (or one whose size changes) would otherwise have every vector it
# produces rejected by from_blob the moment it was stored — dedup silently off,
# nothing logged. The first successful response settles the real length.
_observed_dims: dict[str, int] = {}
# Smaller chunks (well under the per-minute content budget) so one chunk can't
# dump a whole minute's allowance at once: providers count requests over a rolling
# 60s window, so a 90-burst followed by the throttled remainder of a big digest
# used to push the window over the limit and 429 the tail.
_BATCH_LIMIT = 60
_GEMINI_DIMS = 768
_TIMEOUT = aiohttp.ClientTimeout(total=60)
_MAX_RATE_WAIT = 65.0  # cap a single throttle wait; beyond this, let it fail-open
_RETRY_429_WAIT = 50.0  # fallback wait before retrying a rate-limited chunk once
_RETRY_DELAY_RE = re.compile(r'"retryDelay"\s*:\s*"(\d+)s"')

_session: aiohttp.ClientSession | None = None


# Embeddings now ride the same key as the text chains' head provider, so a revoked
# or exhausted Mistral key takes dedup down with it. Acceptable because dedup is
# fail-open and text fails over, but a second embedding provider would need each
# vector to carry which model made it — today only its length distinguishes them.
class _RateLimited(Exception):
    """A 429 from the embedding API: retryable after a short wait, unlike other
    transport errors which fail open immediately."""

    def __init__(self, retry_after: float | None) -> None:
        super().__init__(f"429, retry-after {retry_after or '?'}s")
        self.retry_after = retry_after


# Google tightened the free per-minute budget for gemini-embedding on 2026-09-01:
# even a 3-text chunk now 429s roughly every other pass. Dedup is fail-open, so a
# single failed pass costs nothing but a skipped comparison, and paging the admin
# for each one produced an hourly alert for a condition nobody can act on. Warn
# once when the provider looks genuinely down — several passes in a row with no
# vector at all — and again only after it has recovered in between.
_MAX_QUIET_FAILURES = 4
_consecutive_failures = 0
_outage_reported = False


def _note_pass(ok: int, total: int) -> None:
    """Track whether embedding is failing outright, and alert once per outage."""
    global _consecutive_failures, _outage_reported
    if ok:
        if _outage_reported:
            log.warning("Embeddings recovered: %d/%d vector(s) after an outage", ok, total)
        _consecutive_failures = 0
        _outage_reported = False
        return
    _consecutive_failures += 1
    if _consecutive_failures >= _MAX_QUIET_FAILURES and not _outage_reported:
        _outage_reported = True
        log.warning(
            "Embeddings unavailable: %d consecutive pass(es) returned no vectors — "
            "cross-source dedup and within-source merge are running without embeddings",
            _consecutive_failures,
        )


def _reset_failure_state() -> None:
    """Test hook: clear the outage counters between cases."""
    global _consecutive_failures, _outage_reported
    _consecutive_failures, _outage_reported = 0, False

# Token-bucket rate limiter (a small async queue with backpressure) so embedding
# bursts stay under the provider's per-minute budget instead of hitting 429s.
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


def active_dims() -> int:
    """Vector length the active model produces; stored vectors of any other length
    belong to a previous model and are ignored."""
    model = active_model()
    if model in _observed_dims:
        return _observed_dims[model]
    if settings.embed_provider == "gemini":
        return _GEMINI_DIMS
    return _MODEL_DIMS.get(model, 1024)


def _record_dims(vectors: list[list[float] | None]) -> None:
    """Remember what the provider actually returned, so from_blob keeps the vectors
    we just stored instead of discarding them against a stale table entry."""
    model = active_model()
    sizes = {len(v) for v in vectors if v}
    if len(sizes) != 1:
        return
    size = sizes.pop()
    if _observed_dims.get(model) != size:
        if size != active_dims():
            log.warning("%s returns %d-dim vectors, expected %d — using the returned size",
                        model, size, active_dims())
        _observed_dims[model] = size


def active_model() -> str:
    return settings.gemini_embed_model if settings.embed_provider == "gemini" else settings.embed_model


def to_blob(vec: list[float] | np.ndarray) -> bytes:
    """Serialize a float vector to compact float32 bytes for the items.embedding BLOB."""
    return array("f", (float(x) for x in vec)).tobytes()


def from_blob(blob: bytes | None) -> np.ndarray | None:
    if not blob:
        return None
    vec = np.frombuffer(blob, dtype=np.float32)
    if vec.size != active_dims():
        return None
    return vec


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity of two vectors. Embeddings are not always pre-normalized
    (gemini at reduced dimensions is not), so normalize here. Vectors of different
    lengths come from different models and are not comparable at all: score them 0
    rather than raising, which would abort the whole dedup pass."""
    if a.shape != b.shape:
        return 0.0
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def _request(texts: list[str]) -> tuple[str, dict, dict, dict]:
    """(url, query params, headers, json body) for one chunk on the active provider."""
    if settings.embed_provider == "gemini":
        model = settings.gemini_embed_model
        return (
            _GEMINI_ENDPOINT.format(model=model),
            {"key": settings.gemini_api_key},
            {},
            {"requests": [
                {
                    "model": f"models/{model}",
                    "content": {"parts": [{"text": text or ""}]},
                    "taskType": "SEMANTIC_SIMILARITY",
                    "outputDimensionality": _GEMINI_DIMS,
                }
                for text in texts
            ]},
        )
    return (
        _MISTRAL_ENDPOINT,
        {},
        {"Authorization": f"Bearer {settings.mistral_api_key}"},
        {"model": settings.embed_model, "input": [text or " " for text in texts]},
    )


def _parse(data: dict, count: int) -> list[list[float] | None]:
    """Pull vectors out of a provider response, in input order."""
    if settings.embed_provider == "gemini":
        rows = data.get("embeddings") or []
        vectors = [(e.get("values") if isinstance(e, dict) else None) for e in rows]
    else:
        rows = sorted(data.get("data") or [], key=lambda r: r.get("index", 0))
        vectors = [(r.get("embedding") if isinstance(r, dict) else None) for r in rows]
    if len(vectors) != count:
        log.info("Embed returned %d vectors for %d texts, skipping batch", len(vectors), count)
        return [None] * count
    return vectors


def _api_key() -> str:
    return settings.gemini_api_key if settings.embed_provider == "gemini" else settings.mistral_api_key


async def _embed_chunk(texts: list[str]) -> list[list[float] | None]:
    url, params, headers, payload = _request(texts)
    async with _get_session().post(url, params=params, headers=headers, json=payload) as resp:
        if resp.status != 200:
            body = await resp.text()
            if resp.status == 429:
                # Recoverable: embed_texts retries the chunk once. Log at INFO so a
                # transient 429 we recover from doesn't page the admin through the
                # WARNING-forwarding log handler; a sustained outage warns via _note_pass.
                log.info("Embed 429 on %s (will retry chunk): %s", active_model(), body[:200])
                m = _RETRY_DELAY_RE.search(body)
                raise _RateLimited(float(m.group(1)) if m else None)
            # INFO for the same reason as the 429 above: one failed chunk is
            # fail-open and usually transient, and _note_pass escalates a real outage.
            log.info("Embed failed on %s: %s %s", active_model(), resp.status, body[:300])
            return [None] * len(texts)
        data = await resp.json()
    vectors = _parse(data, len(texts))
    _record_dims(vectors)
    return vectors


async def embed_texts(texts: list[str]) -> list[list[float] | None]:
    """Embed texts in order, chunked under the API batch limit. Returns a
    list aligned with the input; any failed/missing entry is None. Never raises:
    on a missing key or transport error every entry comes back None so dedup is
    skipped, not the items."""
    if not texts:
        return []
    if not _api_key():
        log.info("No API key for embed provider %s, skipping embeddings for %d text(s)",
                 settings.embed_provider, len(texts))
        _note_pass(0, len(texts))
        return [None] * len(texts)

    out: list[list[float] | None] = []
    for i in range(0, len(texts), _BATCH_LIMIT):
        chunk = texts[i:i + _BATCH_LIMIT]
        try:
            await _rate_acquire(len(chunk))
            out.extend(await _embed_chunk(chunk))
        except _RateLimited as rl:
            # The 429 means a big digest's tail spilled over the rolling per-minute
            # window. Wait it out once and retry so dedup/merge get full coverage
            # rather than silently skipping the overflow items.
            wait = min(rl.retry_after or _RETRY_429_WAIT, _MAX_RATE_WAIT)
            log.info("Embed 429 on chunk %d, waiting %.0fs and retrying once", i // _BATCH_LIMIT, wait)
            await asyncio.sleep(wait)
            try:
                await _rate_acquire(len(chunk))
                out.extend(await _embed_chunk(chunk))
            except Exception as exc:
                # Fail-open and self-healing, so INFO: _note_pass decides when a
                # run of these is an outage worth telling the admin about.
                log.info("Embed retry failed on chunk %d: %s", i // _BATCH_LIMIT, exc)
                out.extend([None] * len(chunk))
        except Exception as exc:
            log.info("Embed error on chunk %d: %s", i // _BATCH_LIMIT, exc)
            out.extend([None] * len(chunk))
    ok = sum(1 for v in out if v is not None)
    log.info("Embedded %d/%d text(s) via %s/%s", ok, len(texts), settings.embed_provider, active_model())
    _note_pass(ok, len(texts))
    return out

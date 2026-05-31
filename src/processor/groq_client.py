"""Groq transport layer: the shared AsyncGroq client, a token-bucket rate
limiter, backoff/quota-dead tracking and the single rate-limited JSON call used
by the classifier. Kept separate from prompt/classification logic so the quota
handling can evolve (and be tested) on its own."""
import asyncio
import json
import logging
import time

from groq import AsyncGroq, RateLimitError

from src.config import settings

log = logging.getLogger(__name__)

_client = AsyncGroq(api_key=settings.groq_api_key)

_RATE = 25.0 / 60.0
_CAPACITY = 3.0
_tokens: float = _CAPACITY
_last_refill: float = time.monotonic()
_call_lock = asyncio.Lock()
_backoff_until: dict[str, float] = {}
_quota_dead_until: dict[str, float] = {}
_last_alert_time: float = 0.0
_ALERT_COOLDOWN = 1800.0
_QUOTA_DEAD_THRESHOLD = 300.0

# Per-model usage counters (cumulative since process start) so we can audit
# whether each model is coping: how many calls each served, how often its quota
# died and how many times work fell over to a backup model.
_model_stats: dict[str, dict[str, int]] = {}
_failover_count: int = 0


def _bump(model: str, key: str) -> None:
    stats = _model_stats.setdefault(model, {"ok": 0, "rate_limited": 0, "quota_dead": 0, "error": 0})
    stats[key] += 1


def format_groq_stats() -> str:
    """One-line snapshot of per-model Groq usage for periodic logging."""
    if not _model_stats:
        return "Groq model usage: no calls yet"
    parts = [
        f"{m}[ok={s['ok']} dead={s['quota_dead']} rl={s['rate_limited']} err={s['error']}]"
        for m, s in sorted(_model_stats.items())
    ]
    return "Groq model usage | " + " | ".join(parts) + f" | failovers={_failover_count}"


def reset_groq_stats() -> None:
    """Zero the counters so each logged snapshot reflects only the window since
    the previous one (the totals live on in the logs)."""
    global _failover_count
    _model_stats.clear()
    _failover_count = 0


def _signal_backoff(model: str, seconds: float = 65.0) -> None:
    _backoff_until[model] = time.monotonic() + seconds
    log.debug("Groq rate limit: %s backoff %gs", model, seconds)


def _parse_reset(value: str | None) -> float | None:
    """Parse Groq reset header like '2m59.56s', '15s', '1h30m'."""
    if not value:
        return None
    try:
        total = 0.0
        num = ""
        for ch in value.strip():
            if ch.isdigit() or ch == ".":
                num += ch
            elif ch == "h" and num:
                total += float(num) * 3600
                num = ""
            elif ch == "m" and num:
                total += float(num) * 60
                num = ""
            elif ch == "s" and num:
                total += float(num)
                num = ""
        if num:
            total += float(num)
        return total if total > 0 else None
    except (ValueError, AttributeError):
        return None


def _extract_retry_after(exc) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) if response is not None else None
    if not headers:
        return None
    candidates = [
        headers.get("retry-after"),
        headers.get("x-ratelimit-reset-tokens"),
        headers.get("x-ratelimit-reset-requests"),
    ]
    values = [v for v in (_parse_reset(c) if c else None for c in candidates) if v is not None]
    return max(values) if values else None


def is_quota_dead(model: str | None = None) -> bool:
    target = model or settings.groq_model_classify
    return _quota_dead_until.get(target, 0.0) > time.monotonic()


def _signal_quota_dead(model: str, seconds: float) -> None:
    """Mark one model's daily quota as exhausted. Per-model so a dead primary
    (e.g. 70b) does not block a still-live fallback (e.g. 8b-instant)."""
    _quota_dead_until[model] = time.monotonic() + seconds
    log.warning("Groq quota exhausted for %s: dead for %.0fs (until reset)", model, seconds)


def _refill_tokens() -> None:
    global _tokens, _last_refill
    now = time.monotonic()
    _tokens = min(_CAPACITY, _tokens + (now - _last_refill) * _RATE)
    _last_refill = now


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


def _pick_model(primary: str, fallback_model: str | None) -> str | None:
    """Choose the model to actually call: the primary if its quota is alive,
    otherwise a live fallback, otherwise None when nothing is usable."""
    if not is_quota_dead(primary):
        return primary
    if fallback_model and not is_quota_dead(fallback_model):
        # The death itself is logged once (WARNING) by _signal_quota_dead; this
        # per-call routing line would otherwise repeat for every call in the window.
        log.debug("Groq: %s quota dead, routing to fallback %s", primary, fallback_model)
        return fallback_model
    return None


async def groq_json(messages: list[dict], max_retries: int, model: str | None = None,
                    fallback_model: str | None = None) -> dict:
    """Make a rate-limited Groq JSON-mode call against `model` (defaults to the
    classify model). On per-model quota death it transparently fails over to
    `fallback_model`. Returns the parsed dict, or {} when no model is usable or
    on persistent rate limiting / any other error."""
    global _tokens, _failover_count
    primary = model or settings.groq_model_classify
    effective = _pick_model(primary, fallback_model)
    if effective is None:
        log.debug("Groq: no live model for %s (fallback %s also dead), short-circuiting", primary, fallback_model)
        return {}
    if effective != primary:
        _failover_count += 1
    for attempt in range(max_retries):
        now = time.monotonic()
        wait_until = _backoff_until.get(effective, 0.0)
        if wait_until > now:
            await asyncio.sleep(wait_until - now)

        async with _call_lock:
            _refill_tokens()
            if _tokens < 1.0:
                wait = (1.0 - _tokens) / _RATE
                await asyncio.sleep(wait)
                _refill_tokens()
            _tokens -= 1.0

        try:
            response = await _client.chat.completions.create(
                model=effective,
                messages=messages,
                response_format={"type": "json_object"},
                temperature=0.1,
            )
            parsed = json.loads(response.choices[0].message.content)
            _bump(effective, "ok")
            log.debug("Groq call ok | model=%s", effective)
            return parsed if isinstance(parsed, dict) else {}
        except RateLimitError as exc:
            retry_after = _extract_retry_after(exc)
            if retry_after is not None and retry_after >= _QUOTA_DEAD_THRESHOLD:
                _bump(effective, "quota_dead")
                _signal_quota_dead(effective, retry_after)
                if effective != fallback_model and fallback_model and not is_quota_dead(fallback_model):
                    log.info("Groq: %s quota dead mid-call, failing over to %s", effective, fallback_model)
                    _failover_count += 1
                    effective = fallback_model
                    continue
                await _maybe_send_rate_limit_alert()
                return {}
            _bump(effective, "rate_limited")
            _signal_backoff(effective, retry_after if retry_after else 65.0)
            if attempt < max_retries - 1:
                log.info("Groq rate limit on %s, retrying after backoff (attempt %d/%d)", effective, attempt + 1, max_retries)
            else:
                log.warning("Groq rate limit persistent on %s after %d attempts, using raw-text fallback", effective, max_retries)
                await _maybe_send_rate_limit_alert()
        except Exception as exc:
            _bump(effective, "error")
            log.warning("Groq call error on %s: %s", effective, exc)
            return {}
    return {}

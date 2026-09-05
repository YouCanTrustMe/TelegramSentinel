"""Provider-agnostic LLM transport: one aiohttp path to any OpenAI-compatible
chat-completions endpoint (Groq, Cerebras, Mistral, Zhipu), with per-task model
routing, per-(provider,model) quota-dead/backoff tracking, transparent failover
across SEPARATE provider quotas, JSON-mode + JSON repair, and a per-provider
token-bucket rate limiter.

Evolved from the old single-provider groq_client; the hard-won JSON-repair and
quota/failover logic is preserved, just keyed by "provider/model" and reachable
over any provider. Kept separate from prompt logic so quota handling stays testable.
"""
import asyncio
import json
import logging
import time

import aiohttp

from src.config import settings

log = logging.getLogger(__name__)


# ---- provider registry: base URL, API key, free-tier pacing (rpm/tpm) ----
def _key(name: str) -> str:
    return getattr(settings, f"{name}_api_key", "") or ""


PROVIDERS: dict[str, dict] = {
    # rpm/tpm read off each provider's own x-ratelimit headers (2026-08-31) and paced
    # just under. Groq's 8K tokens/minute is the binding one: a 12-item batch prompt is
    # ~5K tokens, so barely one fits per minute — hence Groq tails rather than heads.
    "groq":     {"base": "https://api.groq.com/openai/v1/chat/completions",        "rpm": 28, "tpm": 8000},
    "mistral":  {"base": "https://api.mistral.ai/v1/chat/completions",             "rpm": 45, "tpm": 50000},
    # Gemini speaks OpenAI's protocol on this path, so it needs no special transport.
    # Free tier is ~15 RPM / 1500 RPD per model; paced just under to leave room for
    # the embedding calls that share the key (a different model, so a separate RPD).
    "gemini":   {"base": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions", "rpm": 14, "tpm": 250000},
    "zhipu":    {"base": "https://open.bigmodel.cn/api/paas/v4/chat/completions",  "rpm": 20, "tpm": 100000},
}

# Per-task routing: ordered (provider, model) chain. Each entry falls over to the
# next on quota death / persistent rate-limit; fallbacks live on SEPARATE provider
# quotas by design, so one exhausted bucket never stalls a task. Groq tails every
# chain as the always-present last resort. Entries whose provider has no API key
# are skipped automatically, so the system degrades gracefully if a key is missing.
TASK_ROUTING: dict[str, list[tuple[str, str]]] = {
    # Re-benchmarked 2026-09-04 on the same 60 prod items, after Mistral's free tier
    # stopped serving its premier models: mistral-small/medium/magistral answer 429
    # ("Rate limit exceeded") to EVERY call, even a single one on an idle key, while
    # ministral-* and mistral-embed on the same key answer 200. It is a per-model cap,
    # not our burst and not the account.
    # ministral-14b heads every chain: 60/60 then 58/60 ids over two runs, 0 bad calls,
    # ~9.5s per 12-item batch — the only candidate that never dropped a call.
    # gemini-3.1-flash-lite is second: same recall as the rest of the Gemini family when
    # it answers, but 8-70x faster than 3.5-flash-lite, whose 20-67s latency is what
    # turned the 2026-09-04 morning digest into a 374s build with re-classify timeouts.
    # Groq tails as a third separate quota (its json_object mode still 400s on gpt-oss,
    # which llm_json repairs from `failed_generation`).
    # Rejected here: mistral-small/medium/large (429/403 — dead for this key),
    # gemini-3.5-flash (2x the latency of 3.1-flash-lite for the same recall),
    # groq gpt-oss-20b and qwen3.6-27b (5/5 bad calls, 400 + 429).
    "classify":  [("mistral", "ministral-14b-latest"), ("gemini", "gemini-3.1-flash-lite"), ("groq", "openai/gpt-oss-120b")],
    # id-array batch summarise (no merge)
    "batch":     [("mistral", "ministral-14b-latest"), ("gemini", "gemini-3.1-flash-lite"), ("groq", "openai/gpt-oss-120b")],
    # id-array grouping + B1 dedup confirm + within-source merge
    "group":     [("mistral", "ministral-14b-latest"), ("gemini", "gemini-3.1-flash-lite"), ("groq", "openai/gpt-oss-120b")],
    # content filter (rate 1-10)
    "filter":    [("mistral", "ministral-14b-latest"), ("gemini", "gemini-3.1-flash-lite"), ("groq", "openai/gpt-oss-120b")],
    # Ukrainian translate-guard retry
    "translate": [("mistral", "ministral-14b-latest"), ("gemini", "gemini-3.1-flash-lite"), ("groq", "openai/gpt-oss-120b")],
}

_QUOTA_DEAD_THRESHOLD = 300.0
_ALERT_COOLDOWN = 1800.0
# How long a 429-without-Retry-After model is skipped, so a digest's later calls route
# past it instead of re-hitting (and re-warning about) the same throttle.
_RATE_LIMIT_COOLDOWN = 60.0
# Consecutive no-Retry-After 429s (nothing succeeding in between) after which a model is
# treated as withdrawn rather than busy: parked for the day and reported once. The streak
# must also SPAN this long, because the filter fires its chunks concurrently — six 429s
# from one burst are contention, six spread over a quarter of an hour are not.
_THROTTLE_STREAK_DEAD = 6
_THROTTLE_STREAK_MIN_SPAN = 900.0
# ...and stay unbroken: a gap this long means the model was serving in between (or was
# simply not called), so the next 429 starts a fresh streak rather than topping up a
# stale one until an unrelated burst pushes it over the line.
_THROTTLE_STREAK_STALE = 3600.0
# A withdrawn model refuses every call, so its strikes arrive as fast as we call it. A
# fallback entry reached only on failover can instead drip a 429 every hour or so without
# ever being withdrawn, so a streak that takes longer than this to build is not evidence.
_THROTTLE_STREAK_MAX_SPAN = 7200.0
# A 402 means the account is out of credit, not out of a rolling quota: nothing
# resets in minutes, so the provider is parked for a day (one alert instead of one
# per call) and re-probed by the daily verify job.
_BILLING_DOWN_SECONDS = 86400.0

# id-array / structured tasks must be deterministic: any sampling raises the chance
# of a dropped or hallucinated id and broken JSON (the source of "N item(s) missing"
# warnings). Free-text summary tasks (classify, translate) keep a little sampling.
_DETERMINISTIC_TASKS = frozenset({"batch", "group", "filter", "translate"})


# ---- JSON repair (provider-agnostic) ----
def _escape_stray_quotes(text: str) -> str:
    """Escape straight double quotes that sit inside a JSON string value (the way
    models break their own JSON, e.g. summary "ставку на "втілену AI""). A quote
    only closes a string when the next non-space char is structural (,:}]) or EOF;
    any other quote inside a string is a stray and gets escaped."""
    out: list[str] = []
    in_str = False
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if in_str and ch == "\\" and i + 1 < n:
            out.append(ch)
            out.append(text[i + 1])
            i += 2
            continue
        if ch == '"':
            if not in_str:
                in_str = True
                out.append(ch)
            else:
                j = i + 1
                while j < n and text[j] in " \t\r\n":
                    j += 1
                if j >= n or text[j] in ",:}]":
                    in_str = False
                    out.append(ch)
                else:
                    out.append('\\"')
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _coerce_json(text: str) -> dict | None:
    """Parse text as a JSON object, retrying once after escaping stray quotes."""
    if not text:
        return None
    for candidate in (text, _escape_stray_quotes(text)):
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _repair_from_groq_400(body: dict) -> dict | None:
    """Recover a usable object from Groq's json_validate_failed error: Groq returns
    the model's malformed output in `failed_generation`, usually valid apart from
    unescaped inner quotes. Other providers just return the JSON, so this is a
    Groq-only branch."""
    err = body.get("error") if isinstance(body, dict) else None
    if not isinstance(err, dict) or err.get("code") != "json_validate_failed":
        return None
    failed = err.get("failed_generation")
    if not isinstance(failed, str) or not failed.strip():
        return None
    return _coerce_json(failed)


def _parse_reset(value: str | None) -> float | None:
    """Parse a reset header like '2m59.56s', '15s', '1h30m', or plain seconds."""
    if not value:
        return None
    try:
        total, num = 0.0, ""
        for ch in value.strip():
            if ch.isdigit() or ch == ".":
                num += ch
            elif ch == "h" and num:
                total += float(num) * 3600; num = ""
            elif ch == "m" and num:
                total += float(num) * 60; num = ""
            elif ch == "s" and num:
                total += float(num); num = ""
        if num:
            total += float(num)
        return total if total > 0 else None
    except (ValueError, AttributeError):
        return None


def _retry_after(headers) -> float | None:
    candidates = [
        headers.get("retry-after"),
        headers.get("x-ratelimit-reset-tokens"),
        headers.get("x-ratelimit-reset-requests"),
        headers.get("x-ratelimit-reset-tokens-minute"),
    ]
    values = [v for v in (_parse_reset(c) if c else None for c in candidates) if v is not None]
    return max(values) if values else None


# ---- per-(provider,model) quota state + stats ----
_backoff_until: dict[str, float] = {}
_quota_dead_until: dict[str, float] = {}
_model_stats: dict[str, dict[str, int]] = {}
_failover_count: int = 0
_last_alert_time: float | None = None
# Consecutive 429s carrying no Retry-After, per model, with no successful call between:
# {tag: (count, first seen, last seen)}.
_throttle_streak: dict[str, tuple[int, float, float]] = {}
# How much of a 200-but-unparseable answer to carry into the log, and how many
# times to re-ask the same model before paying a failover.
_UNPARSEABLE_SNIPPET_CHARS = 200
_UNPARSEABLE_RETRIES = 1
# The deterministic tasks call at temperature 0, where a re-ask is byte-identical
# and so breaks the same way; nudge it just for the retry.
_UNPARSEABLE_RETRY_TEMPERATURE = 0.3


def _tag(provider: str, model: str) -> str:
    return f"{provider}/{model}"


def _bump(tag: str, key: str) -> None:
    stats = _model_stats.setdefault(tag, {"ok": 0, "rate_limited": 0, "quota_dead": 0, "error": 0})
    stats[key] += 1
    if key == "ok":
        _throttle_streak.pop(tag, None)


def format_llm_stats() -> str:
    """One-line snapshot of per-(provider,model) usage for periodic logging."""
    if not _model_stats:
        return "LLM usage: no calls yet"
    parts = [
        f"{t}[ok={s['ok']} dead={s['quota_dead']} rl={s['rate_limited']} err={s['error']}]"
        for t, s in sorted(_model_stats.items())
    ]
    return "LLM usage | " + " | ".join(parts) + f" | failovers={_failover_count}"


def reset_llm_stats() -> None:
    global _failover_count
    _model_stats.clear()
    _failover_count = 0


def _is_dead(tag: str) -> bool:
    return _quota_dead_until.get(tag, 0.0) > time.monotonic()


def _note_throttled(tag: str) -> bool:
    """Count a 429-without-Retry-After and report whether this model now looks retired
    rather than momentarily busy.

    A rolling limit clears in a minute or two. A model whose free tier has been WITHDRAWN
    answers 429 to every call forever — 2026-09-04, mistral-small answered 429 to a single
    call on an idle key, and the 60s cooldown meant the pipeline re-probed it every 20
    minutes all day, each pass paying the failover and logging the same line.

    The streak must span _THROTTLE_STREAK_MIN_SPAN as well as reach its count: the content
    filter gathers its chunks concurrently, so one RPM burst can return six 429s within a
    second, and parking a healthy model for a day over that would be worse than the noise."""
    now = time.monotonic()
    count, first, last = _throttle_streak.get(tag, (0, now, now))
    if now - last > _THROTTLE_STREAK_STALE or now - first > _THROTTLE_STREAK_MAX_SPAN:
        count, first = 0, now
    _throttle_streak[tag] = (count + 1, first, now)
    span = now - first
    return count + 1 >= _THROTTLE_STREAK_DEAD and _THROTTLE_STREAK_MIN_SPAN <= span


def _signal_quota_dead(tag: str, seconds: float) -> None:
    _quota_dead_until[tag] = time.monotonic() + seconds
    log.warning("LLM quota exhausted for %s: dead for %.0fs (until reset)", tag, seconds)


def _signal_backoff(tag: str, seconds: float = 65.0) -> None:
    _backoff_until[tag] = time.monotonic() + seconds
    log.debug("LLM rate limit: %s backoff %gs", tag, seconds)


def _mark_provider_down(provider: str, seconds: float) -> None:
    """Take a whole provider out of routing for a while (e.g. on auth failure):
    auth is provider-level, so mark every model that provider serves as dead so
    later calls skip it instead of re-hitting the bad key on every request."""
    until = time.monotonic() + seconds
    for chain in TASK_ROUTING.values():
        for p, m in chain:
            if p == provider:
                _quota_dead_until[_tag(p, m)] = until


def _resolve_chain(task: str) -> list[tuple[str, str]]:
    """Routing chain for a task, dropping entries whose provider has no API key."""
    chain = TASK_ROUTING.get(task) or TASK_ROUTING["classify"]
    return [(p, m) for (p, m) in chain if _key(p)]


def is_task_dead(task: str) -> bool:
    """True when every usable model in the task's chain is quota-dead — the signal
    to skip an LLM step entirely and fall back to raw text."""
    chain = _resolve_chain(task)
    if not chain:
        return True
    return all(_is_dead(_tag(p, m)) for (p, m) in chain)


# ---- per-provider token-bucket rate limiter ----
_buckets: dict[str, list[float]] = {}  # provider -> [tokens, last_refill]
_bucket_lock = asyncio.Lock()


def _reserve_token(bucket: list[float], now: float, rate: float, capacity: float) -> float:
    """Refill the bucket, take one token — into deficit when it is empty — and return
    how long the caller must wait for the token it just reserved.

    Reserving before the wait is what makes concurrent callers queue: debiting after
    the sleep instead let them all read the same drained bucket, sleep the same
    duration and fire together with the deficit clamped away, which is the 429 burst
    this limiter exists to prevent."""
    bucket[0] = min(capacity, bucket[0] + (now - bucket[1]) * rate)
    bucket[1] = now
    bucket[0] -= 1.0
    return -bucket[0] / rate if bucket[0] < 0 else 0.0


async def _rate_gate(provider: str) -> None:
    rpm = PROVIDERS[provider]["rpm"]
    rate = rpm / 60.0
    capacity = max(2.0, rpm / 10.0)
    async with _bucket_lock:
        now = time.monotonic()
        bucket = _buckets.setdefault(provider, [capacity, now])
        wait = _reserve_token(bucket, now, rate, capacity)
    if wait > 0:
        await asyncio.sleep(wait)


# ---- shared HTTP session ----
_session: aiohttp.ClientSession | None = None
_session_lock = asyncio.Lock()


async def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        async with _session_lock:
            if _session is None or _session.closed:
                _session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120))
    return _session


async def close_session() -> None:
    global _session
    if _session is not None and not _session.closed:
        await _session.close()
    _session = None


async def _maybe_send_rate_limit_alert() -> None:
    global _last_alert_time
    now = time.monotonic()
    if _last_alert_time is not None and now - _last_alert_time < _ALERT_COOLDOWN:
        return
    _last_alert_time = now
    try:
        from src.dispatcher.sender import send_alert
        await send_alert("LLM rate limit exhausted — summaries will fall back to raw text until quota resets")
    except Exception:
        pass


# A provider with no key, or whose key auth-fails (401/403), is dropped from the
# routing chain — but never silently: the admin is alerted, and re-alerted DAILY
# while the problem persists (cooldown < 24h so the daily verify job always fires).
_PROVIDER_ALERT_COOLDOWN = 82800.0  # ~23h
_provider_alert_at: dict[str, float] = {}


async def _alert_provider(provider: str, msg: str) -> None:
    log.warning("LLM provider issue | %s: %s", provider, msg)
    now = time.monotonic()
    last = _provider_alert_at.get(provider)
    if last is not None and now - last < _PROVIDER_ALERT_COOLDOWN:
        return
    _provider_alert_at[provider] = now
    try:
        from src.dispatcher.sender import send_alert
        await send_alert(f"⚠️ LLM provider '{provider}': {msg}")
    except Exception:
        pass


def _routed_providers() -> list[str]:
    """Unique providers the routing actually relies on (order of first appearance)."""
    seen: list[str] = []
    for chain in TASK_ROUTING.values():
        for p, _ in chain:
            if p not in seen:
                seen.append(p)
    return seen


def _sample_model(provider: str) -> str | None:
    for chain in TASK_ROUTING.values():
        for p, m in chain:
            if p == provider:
                return m
    return None


async def _ping(provider: str) -> int | None:
    """Tiny live call; returns the HTTP status (used to distinguish a bad key
    (401/403) from a healthy/quota-limited one). None on network failure."""
    model = _sample_model(provider)
    if not model:
        return None
    try:
        session = await _get_session()
        payload = {"model": model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1}
        headers = {"Authorization": f"Bearer {_key(provider)}", "Content-Type": "application/json"}
        async with session.post(PROVIDERS[provider]["base"], json=payload, headers=headers) as r:
            return r.status
    except Exception as exc:
        log.warning("LLM verify: %s unreachable (%s)", provider, str(exc)[:80])
        return None


async def verify_llm_providers() -> None:
    """Daily health check of every provider the routing relies on. Alerts the admin
    about a missing or invalid/expired API key and repeats the alert daily while it
    stays unresolved. Quota limits (429) are normal and NOT alerted here."""
    for provider in _routed_providers():
        if not _key(provider):
            await _alert_provider(provider, "no API key set — dropped from routing, running on fallbacks")
            continue
        status = await _ping(provider)
        if status in (401, 403):
            await _alert_provider(provider, f"API key invalid or expired (HTTP {status}) — provider temporarily dropped")
        elif status == 402:
            _mark_provider_down(provider, _BILLING_DOWN_SECONDS)
            await _alert_provider(provider, "billing/credit exhausted (HTTP 402) — provider dropped for 24h")
        elif status == 404:
            await _alert_provider(provider, f"model '{_sample_model(provider)}' not found (HTTP 404) — retired? routing entry needs updating")
        elif status is None:
            log.warning("LLM verify: %s unreachable (network) — skipped", provider)
        else:
            log.info("LLM verify: %s key OK (HTTP %d)", provider, status)


async def _call_once(provider: str, model: str, messages: list[dict], temperature: float = 0.1) -> tuple[dict | None, int, object, str]:
    """Single HTTP call. Returns (parsed_or_None, status, headers, raw_snippet).
    parsed is {} on a recoverable empty/error so the caller can distinguish from a
    hard failure via status; raw_snippet is the start of what came back when it
    could not be parsed, so the caller can log the evidence."""
    await _rate_gate(provider)
    session = await _get_session()
    payload = {
        "model": model,
        "messages": messages,
        "response_format": {"type": "json_object"},
        "temperature": temperature,
    }
    headers = {"Authorization": f"Bearer {_key(provider)}", "Content-Type": "application/json"}
    async with session.post(PROVIDERS[provider]["base"], json=payload, headers=headers) as resp:
        status = resp.status
        hdrs = resp.headers
        text = await resp.text()
    if status == 200:
        try:
            data = json.loads(text)
            content = data["choices"][0]["message"]["content"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError):
            return None, status, hdrs, text[:_UNPARSEABLE_SNIPPET_CHARS]
        if not isinstance(content, str):
            # 200 with content null or a non-string: nothing to parse, but the shape
            # itself is the evidence.
            return None, status, hdrs, repr(content)[:_UNPARSEABLE_SNIPPET_CHARS]
        parsed = _coerce_json(content)
        return parsed, status, hdrs, "" if parsed is not None else content[:_UNPARSEABLE_SNIPPET_CHARS]
    if status == 400:
        try:
            body = json.loads(text)
        except json.JSONDecodeError:
            body = {}
        return _repair_from_groq_400(body), status, hdrs, ""
    return None, status, hdrs, ""


async def llm_json(messages: list[dict], max_retries: int = 3, task: str = "classify") -> dict:
    """Route a JSON-mode chat call through the task's (provider, model) chain.
    Each live entry is attempted up to `max_retries` with rate-limit backoff; on
    quota death or persistent rate limiting the call fails over to the next entry
    (a separate provider quota). Returns the parsed dict, or {} when nothing is
    usable / on persistent failure (caller falls back to raw text)."""
    global _failover_count
    chain = _resolve_chain(task)
    if not chain:
        log.warning("LLM: no usable provider for task=%s (no API keys), short-circuiting", task)
        return {}

    temperature = 0.0 if task in _DETERMINISTIC_TASKS else 0.1
    first = True
    for provider, model in chain:
        tag = _tag(provider, model)
        if _is_dead(tag):
            continue
        if not first:
            _failover_count += 1
        first = False

        unparseable_retries = 0
        for attempt in range(max_retries):
            now = time.monotonic()
            wait_until = _backoff_until.get(tag, 0.0)
            if wait_until > now:
                await asyncio.sleep(wait_until - now)
            try:
                call_temperature = (_UNPARSEABLE_RETRY_TEMPERATURE if unparseable_retries
                                    else temperature)
                parsed, status, hdrs, raw = await _call_once(provider, model, messages, call_temperature)
            except Exception as exc:
                _bump(tag, "error")
                log.warning("LLM call error on %s: %s", tag, str(exc)[:120])
                break  # next chain entry

            if status == 200 and parsed is not None:
                _bump(tag, "ok")
                log.debug("LLM call ok | %s", tag)
                return parsed
            if status == 200 and parsed is None:
                _bump(tag, "error")
                if unparseable_retries < _UNPARSEABLE_RETRIES and attempt < max_retries - 1:
                    unparseable_retries += 1
                    # Roughly 1 call in 10 comes back malformed on the current head; the
                    # same model gets it right on the next try, so retry before paying a
                    # failover (and before waking the admin).
                    log.info("LLM %s returned unparseable JSON, retrying same model | raw=%r", tag, raw)
                    continue
                log.warning("LLM %s returned unparseable JSON %d times, failing over | raw=%r",
                            tag, unparseable_retries + 1, raw)
                break  # next chain entry
            if status == 400:
                if parsed is not None:  # repaired from Groq failed_generation
                    _bump(tag, "ok")
                    log.info("LLM json_validate_failed on %s, repaired malformed JSON", tag)
                    return parsed
                _bump(tag, "error")
                log.warning("LLM 400 on %s (json/schema), failing over", tag)
                break  # next chain entry
            if status == 429:
                ra = _retry_after(hdrs)
                if ra is not None and ra >= _QUOTA_DEAD_THRESHOLD:
                    _bump(tag, "quota_dead")
                    _signal_quota_dead(tag, ra)
                    break  # whole model dead → next chain entry
                _bump(tag, "rate_limited")
                if ra is None:
                    # No Retry-After (e.g. Cerebras' 5 RPM cap): fail over now rather than
                    # sleep a guessed wait, and skip this model for a cooldown. A real
                    # Retry-After is still honoured below.
                    if _note_throttled(tag):
                        _bump(tag, "quota_dead")
                        _quota_dead_until[tag] = time.monotonic() + _BILLING_DOWN_SECONDS
                        streak_len = _throttle_streak[tag][0]
                        # Clear it here, not on the next 200: the park's own re-probe is a
                        # single call, and one 429 on it would otherwise re-park the model
                        # for another day, forever.
                        _throttle_streak.pop(tag, None)
                        log.warning("LLM %s has answered 429 to %d calls in a row with nothing in "
                                    "between: treating it as withdrawn, parked for %.0fh — re-bench "
                                    "before routing to it again",
                                    tag, streak_len, _BILLING_DOWN_SECONDS / 3600)
                    else:
                        _quota_dead_until[tag] = time.monotonic() + _RATE_LIMIT_COOLDOWN
                        # INFO, not WARNING: the admin-alert handler forwards WARNING, and
                        # this fires several times a day while the call still succeeds on the
                        # next chain entry. A chain that truly runs out alerts separately.
                        log.info("LLM rate limit on %s (no retry-after), failing over and skipping it for %gs",
                                 tag, _RATE_LIMIT_COOLDOWN)
                    break  # next chain entry
                _signal_backoff(tag, ra)
                if attempt < max_retries - 1:
                    log.info("LLM rate limit on %s, retry %d/%d after %gs backoff (retry-after)", tag, attempt + 1, max_retries, ra)
                    continue
                log.warning("LLM rate limit persistent on %s after %d attempts (%gs retry-after), failing over", tag, max_retries, ra)
                break  # next chain entry
            if status in (401, 403):
                # bad/expired key — retrying is pointless. Take the whole provider
                # out of routing for a while so the rest of the digest skips it
                # instead of re-hitting the bad key on every call; alert + fail over.
                _bump(tag, "error")
                _mark_provider_down(provider, 1800)
                await _alert_provider(provider, f"API key invalid or expired (HTTP {status}) — provider temporarily dropped")
                break  # next chain entry
            if status == 404:
                # the model id is gone — providers retire models (Groq dropped
                # llama-3.3-70b-versatile while it was still the tail of every chain,
                # and nothing noticed because a 404 just looked like a transient error).
                # Park it for a day and alert so a rotted routing entry surfaces.
                _bump(tag, "quota_dead")
                _quota_dead_until[tag] = time.monotonic() + _BILLING_DOWN_SECONDS
                await _alert_provider(f"{provider}:{model}", f"model '{model}' not found (HTTP 404) — retired? routing entry needs updating")
                break  # next chain entry
            if status == 402:
                # out of credit / free tier ended. Retrying costs 3 attempts and a
                # WARNING per call (which the admin-alert handler forwards), so treat
                # it like an auth failure: park the provider for a day, alert once.
                _bump(tag, "quota_dead")
                _mark_provider_down(provider, _BILLING_DOWN_SECONDS)
                await _alert_provider(provider, "billing/credit exhausted (HTTP 402) — provider dropped for 24h")
                break  # next chain entry
            # 5xx / other transient
            _bump(tag, "error")
            if attempt < max_retries - 1:
                await asyncio.sleep(3)
                continue
            log.warning("LLM %s status=%d after %d attempts, failing over", tag, status, max_retries)
            break

    await _maybe_send_rate_limit_alert()
    log.debug("LLM: all providers exhausted for task=%s", task)
    return {}

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
    "groq":     {"base": "https://api.groq.com/openai/v1/chat/completions",        "rpm": 28, "tpm": 11000},
    "cerebras": {"base": "https://api.cerebras.ai/v1/chat/completions",            "rpm": 5,  "tpm": 28000},
    "mistral":  {"base": "https://api.mistral.ai/v1/chat/completions",             "rpm": 45, "tpm": 45000},
    "zhipu":    {"base": "https://open.bigmodel.cn/api/paas/v4/chat/completions",  "rpm": 20, "tpm": 100000},
}

# Per-task routing: ordered (provider, model) chain. Each entry falls over to the
# next on quota death / persistent rate-limit; fallbacks live on SEPARATE provider
# quotas by design, so one exhausted bucket never stalls a task. Groq tails every
# chain as the always-present last resort. Entries whose provider has no API key
# are skipped automatically, so the system degrades gracefully if a key is missing.
TASK_ROUTING: dict[str, list[tuple[str, str]]] = {
    # high-volume single summaries; needs comfortable RPM
    "classify":  [("mistral", "mistral-small-latest"), ("groq", "openai/gpt-oss-120b"), ("groq", "llama-3.3-70b-versatile")],
    # id-array batch summarise (no merge)
    "batch":     [("cerebras", "gpt-oss-120b"), ("mistral", "mistral-small-latest"), ("groq", "llama-3.3-70b-versatile")],
    # id-array grouping + B1 dedup confirm + within-source merge. Mistral leads
    # (50 RPM): on big digests these fan out to many calls and Cerebras (5 RPM)
    # serialised them behind 60s Retry-After walls. Cerebras stays as fallback for
    # its 1M TPD headroom; bench had Mistral drop ~1/25 ids (harmless in B1, where
    # only co-membership matters, and rare in merge after the echo-exact-id prompt).
    "group":     [("mistral", "mistral-small-latest"), ("cerebras", "gpt-oss-120b"), ("groq", "llama-3.3-70b-versatile")],
    # content filter (rate 1-10); strict catchers preferred
    "filter":    [("mistral", "mistral-small-latest"), ("cerebras", "gpt-oss-120b"), ("groq", "llama-3.3-70b-versatile")],
    # Ukrainian translate-guard retry
    "translate": [("mistral", "mistral-small-latest"), ("groq", "openai/gpt-oss-120b"), ("groq", "llama-3.3-70b-versatile")],
}

_QUOTA_DEAD_THRESHOLD = 300.0
_ALERT_COOLDOWN = 1800.0

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


def _tag(provider: str, model: str) -> str:
    return f"{provider}/{model}"


def _bump(tag: str, key: str) -> None:
    stats = _model_stats.setdefault(tag, {"ok": 0, "rate_limited": 0, "quota_dead": 0, "error": 0})
    stats[key] += 1


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


async def _rate_gate(provider: str) -> None:
    rpm = PROVIDERS[provider]["rpm"]
    rate = rpm / 60.0
    capacity = max(2.0, rpm / 10.0)
    async with _bucket_lock:
        now = time.monotonic()
        b = _buckets.setdefault(provider, [capacity, now])
        b[0] = min(capacity, b[0] + (now - b[1]) * rate)
        b[1] = now
        if b[0] < 1.0:
            wait = (1.0 - b[0]) / rate
        else:
            wait = 0.0
            b[0] -= 1.0
    if wait > 0:
        await asyncio.sleep(wait)
        async with _bucket_lock:
            _buckets[provider][0] = max(0.0, _buckets[provider][0] - 1.0)


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
        elif status is None:
            log.warning("LLM verify: %s unreachable (network) — skipped", provider)
        else:
            log.info("LLM verify: %s key OK (HTTP %d)", provider, status)


async def _call_once(provider: str, model: str, messages: list[dict], temperature: float = 0.1) -> tuple[dict | None, int, object]:
    """Single HTTP call. Returns (parsed_or_None, status, headers). parsed is {}
    on a recoverable empty/error so the caller can distinguish from a hard failure
    via status."""
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
            return None, status, hdrs
        return _coerce_json(content), status, hdrs
    if status == 400:
        try:
            body = json.loads(text)
        except json.JSONDecodeError:
            body = {}
        return _repair_from_groq_400(body), status, hdrs
    return None, status, hdrs


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

        for attempt in range(max_retries):
            now = time.monotonic()
            wait_until = _backoff_until.get(tag, 0.0)
            if wait_until > now:
                await asyncio.sleep(wait_until - now)
            try:
                parsed, status, hdrs = await _call_once(provider, model, messages, temperature)
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
                log.warning("LLM %s returned unparseable JSON, failing over", tag)
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
                wait = ra if ra else 65.0
                _signal_backoff(tag, wait)
                src = "retry-after" if ra else "fallback"
                if attempt < max_retries - 1:
                    log.info("LLM rate limit on %s, retry %d/%d after %gs backoff (%s)", tag, attempt + 1, max_retries, wait, src)
                    continue
                log.warning("LLM rate limit persistent on %s after %d attempts (%gs %s), failing over", tag, max_retries, wait, src)
                break  # next chain entry
            if status in (401, 403):
                # bad/expired key — retrying is pointless. Take the whole provider
                # out of routing for a while so the rest of the digest skips it
                # instead of re-hitting the bad key on every call; alert + fail over.
                _bump(tag, "error")
                _mark_provider_down(provider, 1800)
                await _alert_provider(provider, f"API key invalid or expired (HTTP {status}) — provider temporarily dropped")
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

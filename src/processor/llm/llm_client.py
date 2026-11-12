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
import re
import time

import aiohttp

from src.common.util import describe_exc
from src.config import settings

log = logging.getLogger(__name__)


# ---- provider registry: base URL, API key, free-tier pacing (rpm/tpm) ----
def _key(name: str) -> str:
    return getattr(settings, f"{name}_api_key", "") or ""


PROVIDERS: dict[str, dict] = {
    # rpm/tpm read off each provider's own x-ratelimit headers (2026-08-31) and paced
    # just under. Groq's 8K tokens/minute is the binding one: a 12-item batch prompt is
    # ~5K tokens, so barely one fits per minute — hence Groq tails rather than heads.
    # per_model: the provider counts its limits per model, so each model gets its own pace
    # and a fallback to a sibling model does not queue behind the one that just failed.
    "groq":     {"base": "https://api.groq.com/openai/v1/chat/completions",        "rpm": 28, "tpm": 8000, "per_model": True},
    "mistral":  {"base": "https://api.mistral.ai/v1/chat/completions",             "rpm": 45, "tpm": 50000},
    # Gemini speaks OpenAI's protocol on this path, so it needs no special transport.
    # Free tier is ~15 RPM / 1500 RPD per model; paced just under to leave room for
    # the embedding calls that share the key (a different model, so a separate RPD).
    "gemini":   {"base": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions", "rpm": 14, "tpm": 250000, "per_model": True},
    "zhipu":    {"base": "https://open.bigmodel.cn/api/paas/v4/chat/completions",  "rpm": 20, "tpm": 100000},
}

# Per-task routing: ordered (provider, model) chain. Each entry falls over to the
# next on quota death / persistent rate-limit; fallbacks live on SEPARATE provider
# quotas by design, so one exhausted bucket never stalls a task. Entries whose
# provider has no API key are skipped automatically, so the system degrades
# gracefully if a key is missing.
#
# Re-benchmarked 2026-11-02..11 on frozen prod samples, 2 runs per model, every
# disagreement labelled by reading the originals (.bench/mb): 15 free models over
# filter, war, pair, summary, group and route. Where the numbers below come from:
# .bench/mb/score_*.py and the labels_*_2026_11_10 files next to them.
# - gemini-3.1-flash-lite: best or tied-best on every task and never changed a verdict
#   between runs. Google lists its shutdown as 2027-05-07 at the earliest.
# - gemini-3.5-flash-lite: its successor, on its OWN quota, so it sits right behind
#   3.1 and takes over when 3.1 is retired. Weaker: 7-19 flipped verdicts between runs.
#   Same provider, though — both shed load with 503 on the same mornings — so a
#   non-Google model always follows the two.
# - groq qwen3.8-27b: the most precise on war and route (fewest wrong picks, 0 flips);
#   its 8K tokens/minute 429s about every second call under load, so it heads only the
#   small tasks (war: 1-5 calls a digest; route: background).
# - ministral-14b: weakest of the old chain — glued different events into one line
#   (34-37 wrong merges of 48 vs flash-lite's 2) and 7 summary errors of 60 vs 1.
#   Kept behind the Google pair only where it was not worse than the rest.
# Rejected: Cloudflare (gpt-oss ~470 429s and over the 10K-neuron day in one run; qwen3
# invented facts), Kilo gateway models (15-55s per call, 120s timeouts, free «until
# withdrawn»), CF glm-4.7 / Kilo beam / Kilo ling (broken).
FLASH_LITE = ("gemini", "gemini-3.1-flash-lite")
FLASH_LITE_NEXT = ("gemini", "gemini-3.5-flash-lite")
MINISTRAL = ("mistral", "ministral-14b-latest")
QWEN = ("groq", "qwen/qwen3.8-27b")
GPT_OSS = ("groq", "openai/gpt-oss-120b")

TASK_ROUTING: dict[str, list[tuple[str, str]]] = {
    # single-item summarise; not re-benched 11-02, unchanged
    "classify":  [MINISTRAL, FLASH_LITE, GPT_OSS],
    # id-array batch summarise at digest time (the re-classify). Summaries read by eye on
    # 60 posts: flash-lite 1 error, 3.5 1, ministral 7. On 10-18 flash-lite failed under
    # the digest burst here — then 3.5 (own quota) and ministral catch it.
    "batch":     [FLASH_LITE, FLASH_LITE_NEXT, MINISTRAL, GPT_OSS],
    # The background summary pass: 1-3 posts per call, 50-70 calls a day, never a burst.
    "summary":   [FLASH_LITE, FLASH_LITE_NEXT, MINISTRAL, GPT_OSS],
    # Re-writes one summary the fact check flagged; not re-benched 11-02, unchanged
    "recheck":   [FLASH_LITE, GPT_OSS, MINISTRAL],
    # id-array grouping + within-source merge. Right merges / wrong merges of 53 / 48
    # labelled pairs: flash-lite 33/2, qwen3.8 31/1, 3.5 24-30/0, ministral 41/34-37.
    # ministral is left out: a failed call keeps the posts as separate lines, a wrong
    # merge hides a story.
    "group":     [FLASH_LITE, FLASH_LITE_NEXT, QWEN, GPT_OSS],
    # content filter. Hid right / hid wrong of 61 / 48 labelled posts: flash-lite 50/0
    # (0 flips), 3.5 48/2-4, qwen3.8 40/3 (0 flips), ministral 19-23/5-6.
    "filter":    [FLASH_LITE, FLASH_LITE_NEXT, QWEN, MINISTRAL],
    # B1 dedup confirm. Dups called «same» of 80 / wrong of 47 / flips: flash-lite 39/5/0,
    # ministral 39-41/8/3, 3.5 42-43/3/19. Every model finds about half — a prompt matter.
    "pair":      [FLASH_LITE, MINISTRAL, FLASH_LITE_NEXT, GPT_OSS],
    # War reports + the one tally that leads them. Reports caught of 20 / non-war picked /
    # good lead of 17: qwen3.8 18/3/17 (0 flips), flash-lite 20/9/14 (it led with a
    # regional item when no country-wide tally existed), 3.5 20/11-14/16, ministral 8-10/12/15.
    "war":       [QWEN, FLASH_LITE, FLASH_LITE_NEXT, GPT_OSS],
    # Moves a mixed-source story (Hacker News) to another category; shadow, log only.
    # Right moves of 23 / wrong moves of 58: qwen3.8 19-21/3, flash-lite 20/9 (0 flips),
    # 3.5 20-21/5-9, ministral 12-15/10.
    "route":     [QWEN, FLASH_LITE, FLASH_LITE_NEXT],
    # Ukrainian translate-guard retry; not re-benched 11-02, unchanged
    "translate": [MINISTRAL, FLASH_LITE, GPT_OSS],
    # The picture of a post that says little in words (2026-11-12): flash-lite answered 85 of 85 a week's such
    # posts, ~3.6s and ~1.3K tokens each, and read the text on screenshots right; qwen3.8 and ministral read
    # pictures too (checked live), so the chain does not end at Google. ~14 posts a day.
    "image":     [FLASH_LITE, FLASH_LITE_NEXT, QWEN, MINISTRAL],
}

_QUOTA_DEAD_THRESHOLD = 300.0
# A Retry-After longer than this is not slept: the model is skipped for that long and the
# call moves on. Groq asked for 172.8s at 14:45 on 2026-10-08 and 10-12 while mistral was
# out for only 60s, and the filter sat in that sleep — 198s and 296s builds.
_MAX_RETRY_AFTER_WAIT = 30.0
# When every model of a chain is out, but one returns within this long (the 60s
# no-Retry-After cooldown, a skipped Retry-After), the call waits for it and walks the
# chain once more instead of giving up.
_CHAIN_REVIVE_WAIT = 65.0
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
_DETERMINISTIC_TASKS = frozenset({"batch", "summary", "recheck", "group", "filter", "translate", "war", "pair", "route",
                                  "image"})


# Shadow tasks whose answer only feeds a log line: a failure of theirs is logged at INFO
# and never sends the "rate limit exhausted" alert. A model's own health (parked,
# quota dead, bad key) still warns whichever task found it.
# A picture read is an enrichment: when its chain is out, the post is only kept as it was, nothing degrades.
_QUIET_TASKS = frozenset({"route", "image"})


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


# The root key each list-returning task reads its rows from. A model that answers
# with the bare array instead of the documented envelope is answering correctly,
# just one level down, so the array is wrapped rather than thrown away.
_LIST_ROOT_KEY: dict[str, str] = {"batch": "items", "summary": "items", "recheck": "items", "group": "groups",
                                  "filter": "blocked", "war": "war", "pair": "pairs", "route": "route"}


def _coerce_json(text: str, list_key: str | None = None) -> dict | None:
    """Parse text as a JSON object, retrying once after escaping stray quotes.
    A top-level array is wrapped under `list_key` when the task expects rows there."""
    if not text:
        return None
    for candidate in (text, _escape_stray_quotes(text)):
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, list) and list_key:
            return {list_key: parsed}
    return None


def _repair_from_groq_400(body: dict, list_key: str | None = None) -> dict | None:
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
    return _coerce_json(failed, list_key)


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
    """How long a 429 asks us to stay away. A Retry-After header is the provider's own
    answer and wins. Never x-ratelimit-reset-requests: on Groq that is the time until the
    DAILY request bucket is full again (~86s per request used), so taking it turned a
    plain per-minute 429 into a 10-30 min park of a model that answered the next call
    (bench 2026-11-09: «dead for 1642s» while every later call got 200)."""
    explicit = _parse_reset(headers.get("retry-after"))
    if explicit is not None:
        return explicit
    candidates = [
        headers.get("x-ratelimit-reset-tokens"),
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
# An error body is read whole by _error_kind: a busy marker can sit past the first 200 chars.
_ERROR_BODY_CHARS = 4000
_UNPARSEABLE_RETRIES = 1
# The deterministic tasks call at temperature 0, where a re-ask is byte-identical
# and so breaks the same way; nudge it just for the retry.
_UNPARSEABLE_RETRY_TEMPERATURE = 0.3


def _tag(provider: str, model: str) -> str:
    return f"{provider}/{model}"


# Provider capacity, not our quota: Gemini 503 «high demand» (UNAVAILABLE), Mistral 429
# code 3505 «backend_out_of_capacity». Until 2026-11 both logged as a bare status, so a
# morning of Google shedding load read like our quota running out.
_BUSY_STATUSES = frozenset({502, 503, 504, 529})
# Phrases only, never a bare number: a Groq TPM 429 says «Requested 3505» or «try again
# in 1.3505s», and a quota 429 read as busy would never reach the quota-dead logic.
_BUSY_MARKERS = ("high demand", "overloaded", "out of capacity", "out_of_capacity", "capacity exceeded")
_BUSY_CODE_RE = re.compile(r'"code"\s*:\s*"?3505\b')


def _error_kind(status: int, body: str) -> str:
    """'busy' when the provider sheds load, 'quota' for a 429 on our own limits, '' else."""
    text = (body or "").lower()
    if status in _BUSY_STATUSES or (status == 429 and (any(m in text for m in _BUSY_MARKERS)
                                                       or _BUSY_CODE_RE.search(text))):
        return "busy"
    return "quota" if status == 429 else ""


_REMAINING_PREFIX = "x-ratelimit-remaining-"
_last_limits: dict[str, dict[str, str]] = {}


def _limits_key(tag: str) -> str:
    """Mistral counts its limits per key, so every Mistral model shows the one shared
    snapshot instead of each keeping a stale copy of the same bucket."""
    provider, _, model = tag.partition("/")
    return _bucket_key(provider, model) if provider in PROVIDERS else tag


def _note_limits(tag: str, hdrs) -> None:
    """Keep the latest remaining/limit pairs a provider sent (Groq: requests per day and
    tokens per minute; Mistral: per-minute). Gemini sends none."""
    if not hdrs:
        return
    try:
        lowered = {str(k).lower(): str(v) for k, v in hdrs.items()}
    except AttributeError:
        return
    seen = {}
    for name, value in lowered.items():
        if name.startswith(_REMAINING_PREFIX):
            kind = name[len(_REMAINING_PREFIX):]
            limit = lowered.get("x-ratelimit-limit-" + kind)
            seen[kind] = f"{value}/{limit}" if limit else value
    if seen:
        _last_limits[_limits_key(tag)] = seen
        log.debug("LLM limits | %s | %s", tag, " ".join(f"{k}={v}" for k, v in sorted(seen.items())))


def _bump(tag: str, key: str) -> None:
    stats = _model_stats.setdefault(tag, {"ok": 0, "rate_limited": 0, "quota_dead": 0, "error": 0, "busy": 0})
    stats[key] += 1
    if key == "ok":
        _throttle_streak.pop(tag, None)


def format_llm_stats() -> str:
    """One-line snapshot of per-(provider,model) usage for periodic logging."""
    if not _model_stats:
        return "LLM usage: no calls yet"
    parts = []
    for t, s in sorted(_model_stats.items()):
        part = f"{t}[ok={s['ok']} dead={s['quota_dead']} rl={s['rate_limited']} busy={s['busy']} err={s['error']}"
        left = _last_limits.get(_limits_key(t))
        if left:
            part += " left " + " ".join(f"{k}={v}" for k, v in sorted(left.items()))
        parts.append(part + "]")
    return "LLM usage | " + " | ".join(parts) + f" | failovers={_failover_count}"


def reset_llm_stats() -> None:
    global _failover_count
    _model_stats.clear()
    _last_limits.clear()
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


def _revival_wait(chain: list[tuple[str, str]]) -> float | None:
    """Seconds until the first model of a fully-out chain is usable again, when that is
    within _CHAIN_REVIVE_WAIT; None when some model is usable now or none returns soon."""
    now = time.monotonic()
    remaining = [_quota_dead_until.get(_tag(p, m), 0.0) - now for (p, m) in chain]
    if not remaining or min(remaining) <= 0:
        return None
    soonest = min(remaining)
    return soonest if soonest <= _CHAIN_REVIVE_WAIT else None


def is_task_dead(task: str) -> bool:
    """True when every usable model in the task's chain is quota-dead and none returns
    within _CHAIN_REVIVE_WAIT — the signal to skip an LLM step entirely and fall back to
    raw text. A chain out only for a short cooldown is not dead: llm_json waits for it."""
    chain = _resolve_chain(task)
    if not chain:
        return True
    return all(_is_dead(_tag(p, m)) for (p, m) in chain) and _revival_wait(chain) is None


# ---- per-provider token-bucket rate limiter ----
_buckets: dict[str, list[float]] = {}  # provider or provider/model -> [tokens, last_refill]
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


def _bucket_key(provider: str, model: str) -> str:
    return f"{provider}/{model}" if PROVIDERS[provider].get("per_model") else provider


async def _rate_gate(provider: str, model: str) -> None:
    rpm = PROVIDERS[provider]["rpm"]
    rate = rpm / 60.0
    capacity = max(2.0, rpm / 10.0)
    async with _bucket_lock:
        now = time.monotonic()
        bucket = _buckets.setdefault(_bucket_key(provider, model), [capacity, now])
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


def _routed_models(provider: str) -> list[str]:
    """Every model the routing sends to this provider, in first-use order."""
    models: list[str] = []
    for chain in TASK_ROUTING.values():
        for p, m in chain:
            if p == provider and m not in models:
                models.append(m)
    return models


def _sample_model(provider: str) -> str | None:
    models = _routed_models(provider)
    return models[0] if models else None


async def _ping(provider: str, model: str | None = None) -> int | None:
    """Tiny live call; returns the HTTP status (used to distinguish a bad key
    (401/403) from a healthy/quota-limited one). None on network failure."""
    model = model or _sample_model(provider)
    if not model:
        return None
    try:
        session = await _get_session()
        payload = {"model": model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 1}
        headers = {"Authorization": f"Bearer {_key(provider)}", "Content-Type": "application/json"}
        async with session.post(PROVIDERS[provider]["base"], json=payload, headers=headers) as r:
            return r.status
    except Exception as exc:
        # INFO, not WARNING (which reaches the admin): a ping that times out says nothing about
        # the key, and the calls themselves fail over and alert when a whole chain is out.
        log.info("LLM verify: %s unreachable (%s) — skipped", provider, describe_exc(exc))
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
        elif status is not None:
            log.info("LLM verify: %s key OK (HTTP %d)", provider, status)
        if status in (401, 402, 403):
            continue
        # The key answers for one model; a retired or mistyped id of a fallback (3.5
        # flash-lite, groq qwen) would otherwise surface only as a 404 mid-digest.
        for model in _routed_models(provider)[1:]:
            if await _ping(provider, model) == 404:
                await _alert_provider(f"{provider}:{model}",
                                      f"model '{model}' not found (HTTP 404) — retired? routing entry needs updating")


class _ContentRefused(Exception):
    """The provider's own safety filter declined the text (gemini: a 200 with
    finish_reason "content_filter: PROHIBITED_CONTENT" and no message). A re-ask of the
    same model is refused the same way; the next provider in the chain answers it."""


def _refusal(data) -> str | None:
    try:
        reason = data["choices"][0].get("finish_reason")
    except (KeyError, IndexError, TypeError, AttributeError):
        return None
    return reason if isinstance(reason, str) and reason.startswith("content_filter") else None


async def _call_once(provider: str, model: str, messages: list[dict], temperature: float = 0.1,
                     list_key: str | None = None) -> tuple[dict | None, int, object, str]:
    """Single HTTP call. Returns (parsed_or_None, status, headers, raw_snippet).
    parsed is {} on a recoverable empty/error so the caller can distinguish from a
    hard failure via status; raw_snippet is the start of what came back when it
    could not be parsed, so the caller can log the evidence."""
    await _rate_gate(provider, model)
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
        except json.JSONDecodeError:
            return None, status, hdrs, text[:_UNPARSEABLE_SNIPPET_CHARS]
        # gemini drops the message entirely; the OpenAI shape keeps it with content null.
        reason = _refusal(data)
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            if reason:
                raise _ContentRefused(reason)
            return None, status, hdrs, text[:_UNPARSEABLE_SNIPPET_CHARS]
        if not isinstance(content, str):
            if reason:
                raise _ContentRefused(reason)
            # 200 with content null or a non-string: nothing to parse, but the shape
            # itself is the evidence.
            return None, status, hdrs, repr(content)[:_UNPARSEABLE_SNIPPET_CHARS]
        if reason:
            # Cut off by the filter: whatever JSON is there is missing the rest of the rows.
            raise _ContentRefused(reason)
        parsed = _coerce_json(content, list_key)
        return parsed, status, hdrs, "" if parsed is not None else content[:_UNPARSEABLE_SNIPPET_CHARS]
    if status == 400:
        try:
            body = json.loads(text)
        except json.JSONDecodeError:
            body = {}
        return _repair_from_groq_400(body, list_key), status, hdrs, ""
    return None, status, hdrs, text[:_ERROR_BODY_CHARS]


def _failover_level(chain: list[tuple[str, str]], idx: int) -> int:
    """Log level for a transient failure of chain[idx]: INFO while a live model is still
    ahead, since the call heals itself there and WARNING is forwarded to the admin;
    WARNING on the last live model, whose failure makes the caller fall back."""
    return logging.INFO if any(not _is_dead(_tag(p, m)) for p, m in chain[idx + 1:]) else logging.WARNING


async def _walk_chain(chain: list[tuple[str, str]], messages: list[dict], max_retries: int,
                      temperature: float, list_key: str | None, quiet: bool = False) -> dict | None:
    """One pass over the chain: each live entry is attempted up to `max_retries` with
    rate-limit backoff; on quota death or persistent rate limiting the call fails over
    to the next entry (a separate provider quota). None when no entry answered."""
    warn = logging.INFO if quiet else logging.WARNING

    def fail_level(idx: int) -> int:
        return logging.INFO if quiet else _failover_level(chain, idx)

    global _failover_count
    first = True
    for idx, (provider, model) in enumerate(chain):
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
                parsed, status, hdrs, raw = await _call_once(provider, model, messages,
                                                             call_temperature, list_key)
            except _ContentRefused as exc:
                _bump(tag, "error")
                log.log(fail_level(idx), "LLM %s refused the content (%s), failing over", tag, exc)
                break  # next chain entry
            except Exception as exc:
                _bump(tag, "error")
                log.log(fail_level(idx), "LLM call error on %s: %s, failing over", tag, describe_exc(exc))
                break  # next chain entry

            try:
                _note_limits(tag, hdrs)
            except Exception as exc:  # a log helper never costs an answer
                log.debug("LLM limits | %s | unreadable headers: %s", tag, describe_exc(exc))
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
                log.log(warn, "LLM %s returned unparseable JSON %d times, failing over | raw=%r",
                            tag, unparseable_retries + 1, raw)
                break  # next chain entry
            if status == 400:
                if parsed is not None:  # repaired from Groq failed_generation
                    _bump(tag, "ok")
                    log.info("LLM json_validate_failed on %s, repaired malformed JSON", tag)
                    return parsed
                _bump(tag, "error")
                log.log(warn, "LLM 400 on %s (json/schema), failing over", tag)
                break  # next chain entry
            if status == 413:
                # Prompt over the model's per-request or per-minute token size (Groq's 8K
                # TPM against a long morning war chunk): the same prompt fails every retry.
                _bump(tag, "error")
                log.log(fail_level(idx), "LLM 413 on %s (prompt too large for it), failing over", tag)
                break  # next chain entry
            if status == 429 and _error_kind(status, raw) == "busy":
                # The provider is out of capacity, not us out of quota: skip it for the
                # cooldown and never count it toward the «withdrawn» streak — on 2026-11-04
                # a morning of Mistral 3505s parked ministral for 24h as if retired.
                _bump(tag, "busy")
                skip = max(_RATE_LIMIT_COOLDOWN, _retry_after(hdrs) or 0.0)
                _quota_dead_until[tag] = time.monotonic() + skip
                log.log(fail_level(idx), "LLM %s is out of capacity (429, provider busy, not our quota), "
                        "failing over and skipping it for %gs", tag, skip)
                break  # next chain entry
            if status == 429:
                ra = _retry_after(hdrs)
                if ra is not None and ra >= _QUOTA_DEAD_THRESHOLD:
                    _bump(tag, "quota_dead")
                    _signal_quota_dead(tag, ra)
                    break  # whole model dead → next chain entry
                _bump(tag, "rate_limited")
                if ra is not None and ra > _MAX_RETRY_AFTER_WAIT:
                    _quota_dead_until[tag] = time.monotonic() + ra
                    log.info("LLM rate limit on %s asks for %gs, skipping it for that long instead "
                             "of waiting", tag, ra)
                    break  # next chain entry
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
                log.log(warn, "LLM rate limit persistent on %s after %d attempts (%gs retry-after), failing over", tag, max_retries, ra)
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
            busy = _error_kind(status, raw) == "busy"
            _bump(tag, "busy" if busy else "error")
            if attempt < max_retries - 1:
                await asyncio.sleep(3)
                continue
            if busy:
                # The retries stay (they rescue ~130 calls a week), but a model that shed
                # all of them is skipped for the cooldown, so the next call of the digest
                # burst starts on the next provider instead of paying the same retries.
                _quota_dead_until[tag] = time.monotonic() + _RATE_LIMIT_COOLDOWN
            log.log(fail_level(idx), "LLM %s status=%d (%s) after %d attempts, failing over", tag, status,
                    "provider busy, not our quota" if busy else "error", max_retries)
            break
    return None


async def llm_json(messages: list[dict], max_retries: int = 3, task: str = "classify") -> dict:
    """Route a JSON-mode chat call through the task's (provider, model) chain. When the
    whole chain is out but a model returns within _CHAIN_REVIVE_WAIT, wait for it and walk
    the chain once more. Returns the parsed dict, or {} when nothing is usable / on
    persistent failure (caller falls back to raw text)."""
    chain = _resolve_chain(task)
    if not chain:
        log.warning("LLM: no usable provider for task=%s (no API keys), short-circuiting", task)
        return {}

    temperature = 0.0 if task in _DETERMINISTIC_TASKS else 0.1
    list_key = _LIST_ROOT_KEY.get(task)
    for chain_pass in range(2):
        parsed = await _walk_chain(chain, messages, max_retries, temperature, list_key, task in _QUIET_TASKS)
        if parsed is not None:
            return parsed
        wait = _revival_wait(chain)
        if chain_pass or wait is None:
            break
        log.info("LLM: every model for task=%s is rate limited, waiting %.0fs for the first to return",
                 task, wait)
        await asyncio.sleep(wait)

    # Only a chain that stays out alerts: one that was merely busy (10-13: every model in
    # a short cooldown, the digest built in 20.8s) told the admin summaries were falling
    # back to raw text when none did.
    if is_task_dead(task) and task not in _QUIET_TASKS:
        await _maybe_send_rate_limit_alert()
    log.info("LLM: no model answered task=%s, caller falls back", task)
    return {}

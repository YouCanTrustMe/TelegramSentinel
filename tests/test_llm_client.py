"""LLM client: JSON repair (models emit unescaped inner quotes) + task routing.
_escape_stray_quotes / _coerce_json repair malformed JSON; _resolve_chain drops
providers without a key; is_task_dead reports when a whole chain is exhausted."""
import pytest

from src.processor.llm import llm_client
from src.processor.llm.llm_client import (
    _coerce_json,
    _escape_stray_quotes,
    _repair_from_groq_400,
    _resolve_chain,
    is_task_dead,
)


def test_valid_json_passes_through():
    assert _coerce_json('{"a": 1, "b": "x"}') == {"a": 1, "b": "x"}


def test_non_object_returns_none():
    assert _coerce_json("[1, 2, 3]") is None
    assert _coerce_json("totally broken {") is None
    assert _coerce_json("") is None


def test_repairs_real_unescaped_inner_quotes():
    # Production failure mode: straight quotes around «втілену AI» break the JSON.
    bad = (
        '{"items": ['
        '{"id": 1, "summary": "ставку на "втілену AI"", "key_phrase": "Bitcoin"}'
        ']}'
    )
    out = _coerce_json(bad)
    assert out is not None
    assert out["items"][0]["id"] == 1
    assert "втілену AI" in out["items"][0]["summary"]


def test_escape_leaves_clean_json_untouched():
    clean = '{"k": "no inner quotes here"}'
    assert _escape_stray_quotes(clean) == clean


def test_repair_from_groq_400_requires_json_validate_code():
    body = {"error": {"code": "rate_limit", "failed_generation": '{"a": 1}'}}
    assert _repair_from_groq_400(body) is None


def test_repair_from_groq_400_recovers_failed_generation():
    body = {"error": {"code": "json_validate_failed", "failed_generation": '{"summary": "a "b" c"}'}}
    assert _repair_from_groq_400(body) == {"summary": 'a "b" c'}


def test_repair_from_groq_400_handles_missing_body():
    assert _repair_from_groq_400(None) is None
    assert _repair_from_groq_400({}) is None


def test_resolve_chain_drops_keyless_providers(monkeypatch):
    # With external keys blanked, only the always-present Groq tail survives.
    for name in ("cerebras_api_key", "mistral_api_key", "zhipu_api_key"):
        monkeypatch.setattr(llm_client.settings, name, "", raising=False)
    for task in ("classify", "batch", "group", "filter", "translate"):
        chain = _resolve_chain(task)
        assert chain, f"{task} chain empty"
        assert all(p == "groq" for p, _ in chain), f"{task} kept a keyless provider: {chain}"


def test_resolve_chain_keeps_external_when_key_present(monkeypatch):
    monkeypatch.setattr(llm_client.settings, "mistral_api_key", "x-test-key", raising=False)
    providers = [p for p, _ in _resolve_chain("classify")]
    assert "mistral" in providers


def test_unknown_task_falls_back_to_classify_chain():
    assert _resolve_chain("nonexistent") == _resolve_chain("classify")


def test_group_leads_with_high_rpm_provider(monkeypatch):
    # §6 lever: id-heavy `group` must lead with Mistral (50 RPM), not Cerebras
    # (5 RPM) — Cerebras serialised big digests behind 60s Retry-After walls.
    for name in ("cerebras_api_key", "mistral_api_key"):
        monkeypatch.setattr(llm_client.settings, name, "x-test-key", raising=False)
    assert _resolve_chain("group")[0][0] == "mistral"


def test_is_task_dead_false_when_alive():
    assert is_task_dead("group") is False


def test_verify_alerts_on_missing_key(monkeypatch):
    import asyncio
    calls = []
    async def fake_alert(provider, msg):
        calls.append(provider)
    async def fake_ping(provider):
        return 200
    monkeypatch.setattr(llm_client, "_alert_provider", fake_alert)
    monkeypatch.setattr(llm_client, "_ping", fake_ping)
    monkeypatch.setattr(llm_client.settings, "mistral_api_key", "", raising=False)
    monkeypatch.setattr(llm_client.settings, "cerebras_api_key", "x", raising=False)
    asyncio.run(llm_client.verify_llm_providers())
    assert "mistral" in calls  # missing key → alerted
    assert "cerebras" not in calls  # present + ping 200 → no alert


def test_verify_alerts_on_invalid_key(monkeypatch):
    import asyncio
    calls = []
    async def fake_alert(provider, msg):
        calls.append(provider)
    async def fake_ping(provider):
        return 401
    monkeypatch.setattr(llm_client, "_alert_provider", fake_alert)
    monkeypatch.setattr(llm_client, "_ping", fake_ping)
    monkeypatch.setattr(llm_client.settings, "mistral_api_key", "x", raising=False)
    monkeypatch.setattr(llm_client.settings, "cerebras_api_key", "x", raising=False)
    asyncio.run(llm_client.verify_llm_providers())
    assert "mistral" in calls and "cerebras" in calls  # 401 → alerted


def _noop_alert(monkeypatch):
    async def _noop():
        return None
    monkeypatch.setattr(llm_client, "_maybe_send_rate_limit_alert", _noop)


def test_llm_json_returns_parsed_on_success(monkeypatch):
    import asyncio
    llm_client._quota_dead_until.clear()
    monkeypatch.setitem(llm_client.TASK_ROUTING, "t_ok", [("groq", "m1")])
    async def fake_call(provider, model, messages, temperature=0.1):
        return {"summary": "ok"}, 200, {}
    monkeypatch.setattr(llm_client, "_call_once", fake_call)
    out = asyncio.run(llm_client.llm_json([{"role": "user", "content": "x"}], task="t_ok"))
    assert out == {"summary": "ok"}


def test_llm_json_fails_over_to_next_on_quota_dead(monkeypatch):
    import asyncio
    llm_client._quota_dead_until.clear()
    llm_client._failover_count = 0
    monkeypatch.setitem(llm_client.TASK_ROUTING, "t_fo", [("groq", "m1"), ("groq", "m2")])
    calls = []
    async def fake_call(provider, model, messages, temperature=0.1):
        calls.append(model)
        if model == "m1":
            return None, 429, {"retry-after": "600"}  # quota dead → fail over
        return {"ok": 1}, 200, {}
    monkeypatch.setattr(llm_client, "_call_once", fake_call)
    out = asyncio.run(llm_client.llm_json([{"role": "user", "content": "x"}], task="t_fo"))
    assert out == {"ok": 1}
    assert calls == ["m1", "m2"]
    assert llm_client._is_dead("groq/m1") and not llm_client._is_dead("groq/m2")
    assert llm_client._failover_count == 1


def test_llm_json_fails_over_on_unparseable_then_succeeds(monkeypatch):
    import asyncio
    llm_client._quota_dead_until.clear()
    monkeypatch.setitem(llm_client.TASK_ROUTING, "t_bad", [("groq", "m1"), ("groq", "m2")])
    async def fake_call(provider, model, messages, temperature=0.1):
        if model == "m1":
            return None, 200, {}  # 200 but JSON unparseable
        return {"ok": 2}, 200, {}
    monkeypatch.setattr(llm_client, "_call_once", fake_call)
    out = asyncio.run(llm_client.llm_json([{"role": "user", "content": "x"}], task="t_bad"))
    assert out == {"ok": 2}


def test_llm_json_fails_over_immediately_on_429_without_retry_after(monkeypatch):
    """Variant A: a 429 with no Retry-After (e.g. Cerebras' 5 RPM cap) must fail over to
    the next provider at once — no invented backoff sleep, no retries on the throttled
    model — instead of burning ~1-2 min before failing over anyway."""
    import asyncio
    llm_client._quota_dead_until.clear()
    monkeypatch.setitem(llm_client.TASK_ROUTING, "t_ra", [("groq", "m1"), ("groq", "m2")])
    calls = []
    async def fake_call(provider, model, messages, temperature=0.1):
        calls.append(model)
        if model == "m1":
            return None, 429, {}  # throttled, no retry-after header
        return {"ok": 3}, 200, {}
    monkeypatch.setattr(llm_client, "_call_once", fake_call)
    out = asyncio.run(llm_client.llm_json([{"role": "user", "content": "x"}], task="t_ra"))
    assert out == {"ok": 3}
    assert calls == ["m1", "m2"]  # m1 tried once, then immediate failover (not retried)
    assert "groq/m1" not in llm_client._backoff_until  # no invented sleep-backoff registered
    assert llm_client._is_dead("groq/m1")  # skipped for a cooldown so later calls don't re-warn
    llm_client._quota_dead_until.clear()  # don't leak the cooldown into other tests


def test_429_without_retry_after_is_not_admin_alerting(monkeypatch, caplog):
    """A successful failover must not log at WARNING: the admin-alert log handler
    forwards WARNING onwards, and this event fires several times a day while the
    call still completes on the next provider."""
    import asyncio
    import logging
    llm_client._quota_dead_until.clear()
    monkeypatch.setitem(llm_client.TASK_ROUTING, "t_quiet", [("groq", "m1"), ("groq", "m2")])
    async def fake_call(provider, model, messages, temperature=0.1):
        return (None, 429, {}) if model == "m1" else ({"ok": 4}, 200, {})
    monkeypatch.setattr(llm_client, "_call_once", fake_call)
    with caplog.at_level(logging.INFO, logger="src.processor.llm.llm_client"):
        out = asyncio.run(llm_client.llm_json([{"role": "user", "content": "x"}], task="t_quiet"))
    assert out == {"ok": 4}
    messages = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
    assert messages == []
    assert any("no retry-after" in r.message for r in caplog.records)
    llm_client._quota_dead_until.clear()


def test_llm_json_returns_repaired_on_groq_400(monkeypatch):
    import asyncio
    llm_client._quota_dead_until.clear()
    monkeypatch.setitem(llm_client.TASK_ROUTING, "t_rep", [("groq", "m1")])
    async def fake_call(provider, model, messages, temperature=0.1):
        return {"summary": "repaired"}, 400, {}  # _call_once already repaired failed_generation
    monkeypatch.setattr(llm_client, "_call_once", fake_call)
    out = asyncio.run(llm_client.llm_json([{"role": "user", "content": "x"}], task="t_rep"))
    assert out == {"summary": "repaired"}


def test_llm_json_returns_empty_when_all_dead(monkeypatch):
    import asyncio
    _noop_alert(monkeypatch)
    llm_client._quota_dead_until.clear()
    monkeypatch.setitem(llm_client.TASK_ROUTING, "t_dead", [("groq", "d1"), ("groq", "d2")])
    async def fake_call(provider, model, messages, temperature=0.1):
        return None, 429, {"retry-after": "600"}
    monkeypatch.setattr(llm_client, "_call_once", fake_call)
    out = asyncio.run(llm_client.llm_json([{"role": "user", "content": "x"}], task="t_dead"))
    assert out == {}
    assert llm_client.is_task_dead("t_dead") is True


def test_alert_provider_throttles_repeat(monkeypatch):
    import asyncio
    sent = []
    async def fake_send(text):
        sent.append(text)
    monkeypatch.setattr("src.dispatcher.sender.send_alert", fake_send, raising=False)
    llm_client._provider_alert_at.clear()
    asyncio.run(llm_client._alert_provider("mistral", "test issue"))
    asyncio.run(llm_client._alert_provider("mistral", "test issue"))  # within cooldown → suppressed
    assert len(sent) == 1


def test_id_tasks_use_zero_temperature(monkeypatch):
    """Deterministic id-array tasks (batch/group/filter) must call the model at
    temperature 0; free-text summary tasks keep a little sampling."""
    import asyncio
    llm_client._quota_dead_until.clear()
    seen = {}
    async def fake_call(provider, model, messages, temperature=0.1):
        seen[messages[0]["content"]] = temperature
        return {"ok": 1}, 200, {}
    monkeypatch.setattr(llm_client, "_call_once", fake_call)
    monkeypatch.setitem(llm_client.TASK_ROUTING, "group", [("groq", "m1")])
    monkeypatch.setitem(llm_client.TASK_ROUTING, "classify", [("groq", "m1")])
    asyncio.run(llm_client.llm_json([{"role": "user", "content": "g"}], task="group"))
    asyncio.run(llm_client.llm_json([{"role": "user", "content": "f"}], task="filter"))
    asyncio.run(llm_client.llm_json([{"role": "user", "content": "c"}], task="classify"))
    assert seen["g"] == 0.0
    assert seen["f"] == 0.0
    assert seen["c"] == 0.1


def test_llm_json_marks_provider_down_on_auth_fail(monkeypatch):
    """A 401 takes the whole provider out of routing (same key → same failure) and
    fails over to the NEXT provider, so the digest stops re-hitting the bad key."""
    import asyncio
    llm_client._quota_dead_until.clear()
    monkeypatch.setattr(llm_client.settings, "mistral_api_key", "x-test", raising=False)
    monkeypatch.setitem(llm_client.TASK_ROUTING, "t_auth", [("mistral", "m1"), ("groq", "g1")])
    async def noop_alert(provider, msg):
        pass
    calls = []
    async def fake_call(provider, model, messages, temperature=0.1):
        calls.append((provider, model))
        if provider == "mistral":
            return None, 401, {}
        return {"ok": 1}, 200, {}
    monkeypatch.setattr(llm_client, "_alert_provider", noop_alert)
    monkeypatch.setattr(llm_client, "_call_once", fake_call)
    out = asyncio.run(llm_client.llm_json([{"role": "user", "content": "x"}], task="t_auth"))
    assert out == {"ok": 1}
    assert calls == [("mistral", "m1"), ("groq", "g1")]
    assert llm_client._is_dead("mistral/m1") and not llm_client._is_dead("groq/g1")
    llm_client._quota_dead_until.clear()


@pytest.mark.asyncio
async def test_402_parks_the_whole_provider_after_one_attempt(monkeypatch):
    """Cerebras' free tier ended (HTTP 402) and every batch call spent 3 retries plus a
    forwarded WARNING on it for 13 days. 402 is a billing state, not a transient 5xx:
    one attempt, provider parked, one alert, fail over."""
    llm_client._quota_dead_until.clear()
    calls = []
    alerts = []

    async def fake_call_once(provider, model, messages, temperature=0.1):
        calls.append((provider, model))
        if provider == "cerebras":
            return None, 402, {}
        return {"ok": True}, 200, {}

    async def fake_alert(provider, msg):
        alerts.append((provider, msg))

    monkeypatch.setattr(llm_client, "_call_once", fake_call_once)
    monkeypatch.setattr(llm_client, "_alert_provider", fake_alert)
    monkeypatch.setattr(llm_client, "TASK_ROUTING", {
        "batch": [("cerebras", "gpt-oss-120b"), ("groq", "llama-3.3-70b-versatile")],
        "group": [("cerebras", "gpt-oss-120b")],
    }, raising=False)
    for name in ("cerebras_api_key", "groq_api_key"):
        monkeypatch.setattr(llm_client.settings, name, "k", raising=False)

    assert await llm_client.llm_json([{"role": "user", "content": "x"}], task="batch") == {"ok": True}
    assert calls == [("cerebras", "gpt-oss-120b"), ("groq", "llama-3.3-70b-versatile")]
    assert len(alerts) == 1 and "402" in alerts[0][1]
    # provider-level, so the same dead model is skipped on every other chain it heads
    assert llm_client.is_task_dead("group")


@pytest.mark.asyncio
async def test_402_provider_is_skipped_on_later_calls(monkeypatch):
    llm_client._quota_dead_until.clear()
    calls = []

    async def fake_call_once(provider, model, messages, temperature=0.1):
        calls.append(provider)
        return (None, 402, {}) if provider == "cerebras" else ({"ok": True}, 200, {})

    async def fake_alert(provider, msg):
        pass

    monkeypatch.setattr(llm_client, "_call_once", fake_call_once)
    monkeypatch.setattr(llm_client, "_alert_provider", fake_alert)
    monkeypatch.setattr(llm_client, "TASK_ROUTING", {
        "batch": [("cerebras", "gpt-oss-120b"), ("groq", "llama-3.3-70b-versatile")],
    }, raising=False)
    for name in ("cerebras_api_key", "groq_api_key"):
        monkeypatch.setattr(llm_client.settings, name, "k", raising=False)

    for _ in range(3):
        await llm_client.llm_json([{"role": "user", "content": "x"}], task="batch")
    assert calls.count("cerebras") == 1  # parked after the first 402, not re-hit per call

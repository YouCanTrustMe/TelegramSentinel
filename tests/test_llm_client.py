"""LLM client: JSON repair (models emit unescaped inner quotes) + task routing.
_escape_stray_quotes / _coerce_json repair malformed JSON; _resolve_chain drops
providers without a key; is_task_dead reports when a whole chain is exhausted."""
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
    async def fake_call(provider, model, messages):
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
    async def fake_call(provider, model, messages):
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
    async def fake_call(provider, model, messages):
        if model == "m1":
            return None, 200, {}  # 200 but JSON unparseable
        return {"ok": 2}, 200, {}
    monkeypatch.setattr(llm_client, "_call_once", fake_call)
    out = asyncio.run(llm_client.llm_json([{"role": "user", "content": "x"}], task="t_bad"))
    assert out == {"ok": 2}


def test_llm_json_returns_repaired_on_groq_400(monkeypatch):
    import asyncio
    llm_client._quota_dead_until.clear()
    monkeypatch.setitem(llm_client.TASK_ROUTING, "t_rep", [("groq", "m1")])
    async def fake_call(provider, model, messages):
        return {"summary": "repaired"}, 400, {}  # _call_once already repaired failed_generation
    monkeypatch.setattr(llm_client, "_call_once", fake_call)
    out = asyncio.run(llm_client.llm_json([{"role": "user", "content": "x"}], task="t_rep"))
    assert out == {"summary": "repaired"}


def test_llm_json_returns_empty_when_all_dead(monkeypatch):
    import asyncio
    _noop_alert(monkeypatch)
    llm_client._quota_dead_until.clear()
    monkeypatch.setitem(llm_client.TASK_ROUTING, "t_dead", [("groq", "d1"), ("groq", "d2")])
    async def fake_call(provider, model, messages):
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
    async def fake_call(provider, model, messages):
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

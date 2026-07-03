"""_process_message: a post whose media our pinned Pyrogram is too old to decode
(MessageMediaUnsupported — high-level Message exposes nothing) must still be kept
as a 📦 placeholder with a link, while genuinely empty / service messages are
dropped as before."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import src.collectors.telegram_collector as tc
from src.common.media import GENERIC_MEDIA_TOKEN, NO_TEXT

CHAT = "-1002568789348"
SOURCE = {"id": 20, "category": "hrvatska"}


def _msg(**overrides):
    """A pyrogram-Message-shaped stub with everything the collector reads, all
    falsy by default; override per case."""
    base = dict(
        id=239,
        date=datetime(2026, 6, 21, 6, 43, 45),
        poll=None,
        text=None,
        caption=None,
        media=None,
        web_page=None,
        service=None,
        empty=None,
        forward_from_chat=None,
        reply_to_message_id=None,
        media_group_id=None,
    )
    for _attr, _token, _emoji in tc.MEDIA_TYPES:
        base[_attr] = None
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture
def captured(monkeypatch):
    saved = {}

    async def fake_is_duplicate(_mid):
        return False

    async def fake_save_item(**kwargs):
        saved.update(kwargs)

    monkeypatch.setattr(tc, "is_duplicate", fake_is_duplicate)
    monkeypatch.setattr(tc, "save_item", fake_save_item)
    return saved


async def test_unsupported_media_post_kept_as_placeholder(captured):
    # All content None (MessageMediaUnsupported looks empty up here) but not a
    # service/empty message → keep as 📦 placeholder, never drop.
    kept = await tc._process_message(CHAT, SOURCE, _msg())
    assert kept is True
    assert captured["raw_text"] == GENERIC_MEDIA_TOKEN
    assert captured["summary"] == NO_TEXT
    assert captured["original_url"].endswith("/239")


async def test_service_message_dropped(captured):
    kept = await tc._process_message(CHAT, SOURCE, _msg(service="NEW_CHAT_MEMBERS"))
    assert kept is False
    assert captured == {}


async def test_empty_message_dropped(captured):
    kept = await tc._process_message(CHAT, SOURCE, _msg(empty=True))
    assert kept is False
    assert captured == {}


async def test_plain_text_post_still_saved(captured):
    kept = await tc._process_message(CHAT, SOURCE, _msg(text="Нарешті літо в Хорватії"))
    assert kept is True
    assert captured["raw_text"] == "Нарешті літо в Хорватії"
    assert captured["summary"] == ""  # long enough to need classification later


class _StopLoop(Exception):
    """Break the otherwise-infinite keepalive loop from a patched asyncio.sleep."""


async def test_keepalive_single_failure_recovers_without_restart(monkeypatch):
    # One transient ping failure that recovers on the next ping must NOT force a restart.
    state = {"invoke": 0, "restart": 0}

    async def fake_invoke(_):
        state["invoke"] += 1
        if state["invoke"] == 1:
            raise ConnectionError("Connection lost")

    async def fake_restart():
        state["restart"] += 1

    async def fake_sleep(_secs):
        if state["invoke"] >= 2:  # stop once the recovering ping has run
            raise _StopLoop()

    monkeypatch.setattr(tc.userbot, "invoke", fake_invoke)
    monkeypatch.setattr(tc.userbot, "restart", fake_restart)
    monkeypatch.setattr(tc.asyncio, "sleep", fake_sleep)

    with pytest.raises(_StopLoop):
        await tc.keep_userbot_online()

    assert state["restart"] == 0
    assert state["invoke"] == 2


async def test_keepalive_floodwait_does_not_force_restart(monkeypatch):
    # A FloodWait on the ping is a rate-limit, not a dead connection: it must never
    # count toward the failure threshold nor trigger a restart.
    from pyrogram.errors import FloodWait

    state = {"invoke": 0, "restart": 0}

    async def fake_invoke(_):
        state["invoke"] += 1
        raise FloodWait(value=7)

    async def fake_restart():
        state["restart"] += 1

    async def fake_sleep(_secs):
        if state["invoke"] >= 3:  # let several FloodWaits go by, then stop
            raise _StopLoop()

    monkeypatch.setattr(tc.userbot, "invoke", fake_invoke)
    monkeypatch.setattr(tc.userbot, "restart", fake_restart)
    monkeypatch.setattr(tc.asyncio, "sleep", fake_sleep)

    with pytest.raises(_StopLoop):
        await tc.keep_userbot_online()

    assert state["restart"] == 0


async def test_keepalive_forces_restart_after_consecutive_failures(monkeypatch):
    # Every ping fails → after _KEEPALIVE_FAIL_LIMIT in a row, force one restart,
    # and re-check on the short retry interval (not the full keepalive interval).
    calls = {"invoke": 0, "restart": 0, "alerts": 0, "sleeps": []}

    async def fake_invoke(_):
        calls["invoke"] += 1
        raise ConnectionError("Connection lost")

    async def fake_restart():
        calls["restart"] += 1

    async def fake_alert(*_a, **_k):
        calls["alerts"] += 1

    async def fake_sleep(secs):
        calls["sleeps"].append(secs)
        if calls["restart"] >= 1:  # stop right after the forced restart
            raise _StopLoop()

    monkeypatch.setattr(tc.userbot, "invoke", fake_invoke)
    monkeypatch.setattr(tc.userbot, "restart", fake_restart)
    monkeypatch.setattr(tc, "admin_alert", fake_alert)
    monkeypatch.setattr(tc.asyncio, "sleep", fake_sleep)

    with pytest.raises(_StopLoop):
        await tc.keep_userbot_online()

    assert calls["restart"] == 1
    assert calls["alerts"] == 1
    assert calls["invoke"] == tc._KEEPALIVE_FAIL_LIMIT
    assert calls["sleeps"][0] == tc._KEEPALIVE_RETRY_INTERVAL

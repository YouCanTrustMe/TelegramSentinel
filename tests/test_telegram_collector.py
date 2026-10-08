"""_process_message: a post whose media our pinned Pyrogram is too old to decode
(MessageMediaUnsupported — high-level Message exposes nothing) must still be kept
as a 📦 placeholder with a link, while genuinely empty / service messages are
dropped as before."""
from datetime import datetime
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


async def test_keepalive_tick_success_resets_counter(monkeypatch):
    # A healthy ping clears any accumulated failures and leaves the cooldown untouched.
    async def ok(_):
        return None

    async def never_restart():
        raise AssertionError("must not restart on a healthy ping")

    monkeypatch.setattr(tc.userbot, "invoke", ok)
    monkeypatch.setattr(tc.userbot, "restart", never_restart)

    failures, last_restart = await tc._keepalive_tick(3, 123.0)
    assert failures == 0
    assert last_restart == 123.0


async def test_keepalive_tick_single_failure_no_restart(monkeypatch):
    # One failure only bumps the counter; below the threshold nothing is restarted.
    async def boom(_):
        raise ConnectionError("Connection lost")

    calls = {"restart": 0}

    async def fake_restart():
        calls["restart"] += 1

    monkeypatch.setattr(tc.userbot, "invoke", boom)
    monkeypatch.setattr(tc.userbot, "restart", fake_restart)

    failures, _ = await tc._keepalive_tick(0, 0.0)
    assert failures == 1
    assert calls["restart"] == 0


async def test_keepalive_tick_forces_restart_at_threshold(monkeypatch):
    # Reaching _KEEPALIVE_FAIL_LIMIT forces exactly one restart + one admin alert,
    # resets the counter, and stamps the cooldown.
    async def boom(_):
        raise ConnectionError("Connection lost")

    calls = {"restart": 0, "alerts": 0}

    async def fake_restart():
        calls["restart"] += 1

    async def fake_alert(*_a, **_k):
        calls["alerts"] += 1

    monkeypatch.setattr(tc.userbot, "invoke", boom)
    monkeypatch.setattr(tc.userbot, "restart", fake_restart)
    monkeypatch.setattr(tc, "admin_alert", fake_alert)

    # last_restart=None → no prior restart, so the cooldown must never block the first one.
    failures, last_restart = await tc._keepalive_tick(tc._KEEPALIVE_FAIL_LIMIT - 1, None)
    assert calls["restart"] == 1
    assert calls["alerts"] == 1
    assert failures == 0
    assert last_restart is not None


async def test_keepalive_tick_cooldown_blocks_second_restart(monkeypatch):
    # At the threshold but still inside the cooldown window → no restart (anti-storm).
    async def boom(_):
        raise ConnectionError("Connection lost")

    calls = {"restart": 0}

    async def fake_restart():
        calls["restart"] += 1

    monkeypatch.setattr(tc.userbot, "invoke", boom)
    monkeypatch.setattr(tc.userbot, "restart", fake_restart)

    just_restarted = tc.time.monotonic()  # last restart ~now → within cooldown
    failures, _ = await tc._keepalive_tick(tc._KEEPALIVE_FAIL_LIMIT, just_restarted)
    assert calls["restart"] == 0
    assert failures == tc._KEEPALIVE_FAIL_LIMIT + 1


async def test_keepalive_tick_floodwait_not_counted(monkeypatch):
    # A FloodWait is a rate-limit, not a dead connection: counter unchanged, no restart.
    from pyrogram.errors import FloodWait

    async def flood(_):
        raise FloodWait(value=7)

    async def never_restart():
        raise AssertionError("FloodWait must not trigger a restart")

    monkeypatch.setattr(tc.userbot, "invoke", flood)
    monkeypatch.setattr(tc.userbot, "restart", never_restart)

    failures, _ = await tc._keepalive_tick(1, 0.0)
    assert failures == 1


class _FailingHistory:
    def __init__(self, fail):
        self.fail = fail

    def get_chat_history(self, _chat_id, limit):
        async def gen():
            if self.fail is True:
                raise tc.InternalServerError("RPC_CALL_FAIL")
            if self.fail:
                raise self.fail
            return
            yield
        return gen()


async def _poll(monkeypatch, fail):
    monkeypatch.setattr(tc, "userbot", _FailingHistory(fail))
    return await tc._poll_channel(CHAT, {"id": 77, "name": "Chan", "last_message_id": 5})


async def test_telegram_5xx_stays_quiet_until_it_repeats(monkeypatch, caplog):
    # A single Telegram-side 500 heals on the next poll; only the Nth consecutive
    # one on the same source reaches WARNING, which the admin channel forwards.
    monkeypatch.setattr(tc, "_server_error_streak", {})
    caplog.set_level("INFO", logger=tc.log.name)
    for _ in range(tc._SERVER_ERROR_WARN_STREAK - 1):
        await _poll(monkeypatch, fail=True)
    assert not [r for r in caplog.records if r.levelname in ("WARNING", "ERROR")]
    await _poll(monkeypatch, fail=True)
    assert [r.levelname for r in caplog.records if r.levelname != "INFO"] == ["WARNING"]


async def test_telegram_5xx_streak_resets_on_a_good_poll(monkeypatch):
    monkeypatch.setattr(tc, "_server_error_streak", {})
    await _poll(monkeypatch, fail=True)
    await _poll(monkeypatch, fail=True)
    assert tc._server_error_streak[77][0] == 2
    await _poll(monkeypatch, fail=False)
    assert tc._server_error_streak == {}


async def test_any_other_poll_outcome_breaks_the_streak(monkeypatch):
    # A FloodWait or any other error between two 500s means they were not in a row.
    monkeypatch.setattr(tc, "_server_error_streak", {})
    await _poll(monkeypatch, fail=True)
    await _poll(monkeypatch, fail=RuntimeError("other"))
    assert tc._server_error_streak == {}


async def test_a_503_counts_as_telegram_side_too(monkeypatch):
    monkeypatch.setattr(tc, "_server_error_streak", {})
    await _poll(monkeypatch, fail=tc.ServiceUnavailable("unavailable"))
    assert tc._server_error_streak[77][0] == 1


def test_a_channel_that_never_heals_warns_again_every_few_hours():
    first = tc._SERVER_ERROR_WARN_STREAK
    warned = [n for n in range(1, first + 2 * tc._SERVER_ERROR_REWARN_EVERY + 1)
              if tc._server_error_level(n) == tc.logging.WARNING]
    assert warned == [first, first + tc._SERVER_ERROR_REWARN_EVERY, first + 2 * tc._SERVER_ERROR_REWARN_EVERY]


async def test_an_old_streak_does_not_carry_into_a_later_failure(monkeypatch):
    # A source paused mid-streak is never polled, so nothing clears it; a 500 a
    # week after resuming must start from one, not warn as the sixth in a row.
    monkeypatch.setattr(tc, "_server_error_streak",
                        {77: (tc._SERVER_ERROR_WARN_STREAK - 1, tc.time.monotonic() - 7 * 86400)})
    await _poll(monkeypatch, fail=True)
    assert tc._server_error_streak[77][0] == 1


class _History:
    def __init__(self, messages):
        self.messages = messages

    def get_chat_history(self, _chat_id, limit):
        async def gen():
            for message in self.messages:
                yield message
        return gen()


async def test_old_posts_are_skipped_and_a_catch_up_gap_is_reported(monkeypatch):
    """2026-10-07: the central bank channel's first poll took 20 posts, 19 of them older than two days."""
    from datetime import timedelta

    now = datetime.now()
    messages = [_msg(id=30, date=now - timedelta(hours=1), text="fresh", chat=None),
                _msg(id=20, date=now - timedelta(days=3), text="old", chat=None),
                _msg(id=10, date=now - timedelta(days=20), text="older", chat=None)]
    processed, bookmark = [], {}

    async def fake_process(chat_ref, source, message, parent_msg=None):
        processed.append(message.id)
        return True

    async def fake_bookmark(source_id, msg_id):
        bookmark[source_id] = msg_id

    monkeypatch.setattr(tc, "userbot", _History(messages))
    monkeypatch.setattr(tc, "_process_message", fake_process)
    monkeypatch.setattr(tc, "set_source_last_message_id", fake_bookmark)
    monkeypatch.setattr(tc, "_server_error_streak", {})
    gaps = []

    saved = await tc._poll_channel(CHAT, {"id": 91, "name": "NBU", "last_message_id": None, "chat_id": 1}, gaps)
    assert saved == 1 and processed == [30]
    assert bookmark == {91: 30}
    assert gaps == []   # a new channel's history is expected, not a loss

    # A catch-up that finds posts past the limit means news was lost unseen.
    processed.clear()
    messages.append(_msg(id=9, date=now - timedelta(days=4), service="pinned_message", chat=None))
    saved = await tc._poll_channel(CHAT, {"id": 91, "name": "NBU", "last_message_id": 5, "chat_id": 1}, gaps)
    assert processed == [30]
    assert gaps == [("NBU", 2)]   # the service message is not a lost post


async def test_a_gap_in_many_channels_is_reported_in_one_message(monkeypatch):
    """An outage hits every channel at once; one alert per source arrived as a burst."""
    sent = []

    async def fake_sources(type_=None):
        return [{"id": n, "name": f"A&B {n}", "category": "feed", "url": f"@c{n}", "chat_id": n,
                 "last_message_id": 1} for n in (1, 2)]

    async def fake_poll(chat_ref, source, gaps):
        gaps.append((source["name"], 3))
        return 0

    async def fake_alert(text, key=None, silent=True):
        sent.append((key, text))

    monkeypatch.setattr(tc, "get_active_sources", fake_sources)
    monkeypatch.setattr(tc, "_poll_channel", fake_poll)
    monkeypatch.setattr(tc, "admin_alert", fake_alert)
    await tc.poll_telegram_once()
    assert len(sent) == 1 and sent[0][0] == "stale_catchup:A&B 1,A&B 2"
    assert "A&amp;B 1: 3" in sent[0][1] and "A&amp;B 2: 3" in sent[0][1]


async def test_a_forward_is_labelled_in_either_library_shape(captured):
    """Newer pyrogram forks dropped forward_from_chat for forward_origin; reading the old
    name there raised on every post, which would stop collection outright."""
    old = _msg(text="Body text", forward_from_chat=SimpleNamespace(title="Source"))
    assert await tc._process_message(CHAT, SOURCE, old)
    assert captured["raw_text"] == "[Forwarded from Source] Body text"

    new = _msg(text="Body text")
    del new.forward_from_chat
    new.forward_origin = SimpleNamespace(chat=SimpleNamespace(title="Source"))
    assert await tc._process_message(CHAT, SOURCE, new)
    assert captured["raw_text"] == "[Forwarded from Source] Body text"

    plain = _msg(text="Body text")
    del plain.forward_from_chat
    plain.forward_origin = None
    assert await tc._process_message(CHAT, SOURCE, plain)
    assert captured["raw_text"] == "Body text"

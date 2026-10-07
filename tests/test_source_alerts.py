"""Source-health alerts against a real SQLite file: the 🔕 opt-out column the 14-day
silent push reads, and the RSS re-probe that tells a feed's own short outage (heals
in the hour, says nothing) from one still down hours later."""
import pytest

from src.config import settings
from src.db.base import get_db, init_db
from src.db.sources import (
    add_source,
    get_silent_sources,
    revive_error_rss_sources,
    set_source_silent_alert_muted,
)


@pytest.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", str(tmp_path / "sentinel.db"))
    await init_db()


async def _rss(name: str, fails: int) -> int:
    sid = await add_source("rss", name, f"https://{name}.example/feed", "ai")
    async with get_db() as conn:
        await conn.execute("UPDATE sources SET status='error', fail_count=? WHERE id=?", (fails, sid))
        await conn.commit()
    return sid


async def test_the_frequent_reprobe_only_revives_feeds_down_for_a_few_hours(db):
    await _rss("fresh", 4)
    await _rss("stale", 30)

    assert await revive_error_rss_sources(12) == ["fresh"]


async def test_the_daily_reprobe_revives_every_failed_feed(db):
    await _rss("fresh", 4)
    await _rss("stale", 30)

    assert sorted(await revive_error_rss_sources()) == ["fresh", "stale"]


async def test_a_muted_source_leaves_every_quiet_list(db):
    """🔕 on a rarely-posting channel: no 14-day push, and not listed as quiet on the
    home screen, /stats or the digest — all three read get_silent_sources."""
    muted = await add_source("telegram", "How To Onchain?", "@howtoonchain", "crypto")
    await add_source("telegram", "ХОРВАТСЬКА ЛЕГКО", "@hrlegko", "hrvatska")
    await set_source_silent_alert_muted(muted, True)

    assert [r["name"] for r in await get_silent_sources(0)] == ["ХОРВАТСЬКА ЛЕГКО"]

    await set_source_silent_alert_muted(muted, False)
    assert sorted(r["name"] for r in await get_silent_sources(0)) == ["How To Onchain?", "ХОРВАТСЬКА ЛЕГКО"]


def test_tapping_one_mute_button_removes_only_that_sources_row():
    """Feed the real alert's buttons into the real handler helper: the producer and the
    consumer of `silent_mute:<id>` must agree, not just each look right on its own."""
    import re

    from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

    from src.bot.handlers.sources import _without_button
    from src.scheduler import _silent_alert

    rows = [{"id": 7, "name": "How To Onchain?", "type": "telegram", "last_item_at": None, "hours_silent": None},
            {"id": 8, "name": "ХОРВАТСЬКА ЛЕГКО", "type": "telegram", "last_item_at": None, "hours_silent": None}]
    _, payload = _silent_alert(rows)
    markup = InlineKeyboardMarkup([[InlineKeyboardButton(b["text"], callback_data=b["callback_data"]) for b in row]
                                   for row in payload["inline_keyboard"]])
    tapped = payload["inline_keyboard"][0][0]["callback_data"]

    assert re.match(r"^silent_mute:", tapped) and int(tapped.split(":", 1)[1]) == 7
    left = _without_button(markup, tapped)
    assert [b.callback_data for row in left.inline_keyboard for b in row] == ["silent_mute:8"]
    assert _without_button(left, "silent_mute:8") is None


async def test_an_undelivered_silent_push_is_retried_next_run(monkeypatch):
    import src.dispatcher.sender as sender
    import src.db.models as models
    from src import scheduler

    memo = {"silent_sources_alerted": ""}

    async def fake_rows(_h):
        return [{"id": 7, "name": "How To Onchain?", "type": "telegram", "last_item_at": None,
                 "hours_silent": None, "created_at": "2026-01-01"}]

    async def get_setting(key):
        return memo.get(key)

    async def set_setting(key, value):
        memo[key] = value

    async def failed_send(*a, **kw):
        return False

    monkeypatch.setattr(models, "get_silent_sources", fake_rows)
    monkeypatch.setattr(models, "get_app_setting", get_setting)
    monkeypatch.setattr(models, "set_app_setting", set_setting)
    monkeypatch.setattr(sender, "send_to", failed_send)

    await scheduler._silent_sources_job()

    assert memo["silent_sources_alerted"] == ""


async def test_many_silent_sources_go_out_in_chunks_that_fit_telegram(monkeypatch):
    import src.dispatcher.sender as sender
    import src.db.models as models
    from src import scheduler

    sent: list[dict] = []

    async def fake_rows(_h):
        return [{"id": i, "name": f"src{i}", "type": "telegram", "last_item_at": None,
                 "hours_silent": None, "created_at": "2026-01-01"} for i in range(1, 46)]

    async def get_setting(key):
        return ""

    async def set_setting(key, value):
        pass

    async def fake_send(chat_id, text, reply_markup=None):
        sent.append(reply_markup)
        return True

    monkeypatch.setattr(models, "get_silent_sources", fake_rows)
    monkeypatch.setattr(models, "get_app_setting", get_setting)
    monkeypatch.setattr(models, "set_app_setting", set_setting)
    monkeypatch.setattr(sender, "send_to", fake_send)

    await scheduler._silent_sources_job()

    assert [len(m["inline_keyboard"]) for m in sent] == [20, 20, 5]


async def test_a_network_error_mid_push_still_records_what_was_delivered(monkeypatch):
    import src.dispatcher.sender as sender
    import src.db.models as models
    from src import scheduler

    memo = {"silent_sources_alerted": ""}
    calls = {"n": 0}

    async def fake_rows(_h):
        return [{"id": i, "name": f"src{i}", "type": "telegram", "last_item_at": None,
                 "hours_silent": None, "created_at": "2026-01-01"} for i in range(1, 26)]

    async def get_setting(key):
        return memo.get(key)

    async def set_setting(key, value):
        memo[key] = value

    async def flaky_send(chat_id, text, reply_markup=None):
        calls["n"] += 1
        if calls["n"] == 2:
            raise TimeoutError
        return True

    monkeypatch.setattr(models, "get_silent_sources", fake_rows)
    monkeypatch.setattr(models, "get_app_setting", get_setting)
    monkeypatch.setattr(models, "set_app_setting", set_setting)
    monkeypatch.setattr(sender, "send_to", flaky_send)

    await scheduler._silent_sources_job()

    assert memo["silent_sources_alerted"] == ",".join(str(i) for i in range(1, 21))

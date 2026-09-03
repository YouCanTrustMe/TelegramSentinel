"""The home screen renders from a plain state dict, so the screen the admin opens
on every visit is checked here without a database or a live bot."""
from datetime import datetime

import pytest

from datetime import timezone
from zoneinfo import ZoneInfo

from src.bot.home import _countdown, home_keyboard, home_text, local_sent_at, next_fire


def _at(hour, minute=0):
    return datetime(2026, 8, 31, hour, minute)


def test_next_fire_picks_the_upcoming_slot():
    assert next_fire(["09:00", "14:00", "19:00"], _at(16, 20)) == ("19:00", 160)


def test_next_fire_wraps_past_midnight():
    slot, minutes = next_fire(["09:00", "19:00"], _at(21, 0))
    assert slot == "09:00"
    assert minutes == 12 * 60


def test_next_fire_skips_the_slot_happening_right_now():
    # A slot at the current minute has already fired; the countdown must not read 0.
    assert next_fire(["09:00", "14:00"], _at(9, 0)) == ("14:00", 300)


def test_next_fire_without_a_schedule():
    assert next_fire([], _at(10)) is None


def test_countdown_formats_hours_and_minutes():
    assert _countdown(160) == "2h 40m"
    assert _countdown(45) == "45m"
    assert _countdown(-5) == "0m"


def test_last_digest_is_read_in_the_digest_timezone():
    # digest_log.sent_at is UTC; the screen speaks the digest timezone, so a
    # 07:00Z digest is the 09:00 Berlin one — same issue number, local clock.
    local = local_sent_at("2026-08-31 07:00:00", ZoneInfo("Europe/Berlin"))
    assert local.strftime("%H:%M") == "09:00"
    assert local.timetuple().tm_yday == 243


def test_last_digest_past_midnight_keeps_the_local_issue_number():
    # 23:30Z on day 243 is already day 244 in Berlin, and that is the number the
    # digest itself printed.
    local = local_sent_at("2026-08-31 23:30:00", ZoneInfo("Europe/Berlin"))
    assert local.timetuple().tm_yday == 244


def test_last_digest_with_an_explicit_offset_is_not_shifted_twice():
    local = local_sent_at("2026-08-31T07:00:00+00:00", ZoneInfo("Europe/Berlin"))
    assert local.strftime("%H:%M") == "09:00"


def test_unparsable_sent_at_is_dropped_rather_than_crashing_the_screen():
    assert local_sent_at("garbage", timezone.utc) is None


def _state(**over):
    state = {
        "next": ("19:00", 160), "pending": 34, "categories": 5, "sources": 41,
        "filters": 12, "last_digest": {"issue": 243, "time": "14:00", "items": 61},
        "errored": 0,
        "paused": 0,
        "quiet": [],
    }
    state.update(over)
    return state


def test_home_text_reports_a_healthy_pipeline():
    text = home_text(_state())
    assert "all clear" in text
    assert "Next digest <b>19:00</b> · in 2h 40m" in text
    assert "<b>34</b> waiting · 5 categories · 41 sources" in text
    assert "Last: <b>#243 14:00</b> · 61 items" in text


def test_a_failing_source_outranks_a_quiet_one_in_the_status():
    # "all clear" beside a feed the collector gave up on was the worst case.
    text = home_text(_state(errored=2, quiet=[("Import AI", 11)]))
    assert "⚠️ 2 failing" in text


def test_home_text_names_the_quiet_sources():
    text = home_text(_state(quiet=[("Import AI", 11), ("Index.hr", 6)]))
    assert "💤 2 quiet" in text
    assert "Import AI 11d · Index.hr 6d" in text


def test_a_source_that_never_produced_says_never_not_zero_days():
    text = home_text(_state(quiet=[("Brand new feed", None)]))
    assert "Brand new feed never" in text
    assert "0d" not in text


def test_home_text_truncates_a_long_quiet_list():
    quiet = [(f"src{i}", i) for i in range(1, 6)]
    assert "+2" in home_text(_state(quiet=quiet))


def test_home_text_flags_an_empty_schedule():
    text = home_text(_state(next=None))
    assert "⚠️ nothing scheduled" in text
    assert "set one in 🕐 Timetable" in text


def test_home_text_singularises_counts():
    text = home_text(_state(categories=1, sources=1,
                            last_digest={"issue": 1, "time": "09:00", "items": 1}))
    assert "1 category · 1 source" in text
    assert "1 item" in text and "1 items" not in text


def test_home_text_survives_a_first_run_with_no_digest_yet():
    text = home_text(_state(last_digest=None, categories=0, next=None))
    assert "Last:" not in text
    assert "no categories yet" in text


@pytest.mark.parametrize("filters,label", [(12, "🚫 Filters · 12"), (0, "🚫 Filters")])
def test_home_keyboard_carries_counts(filters, label):
    kb = home_keyboard(_state(filters=filters))
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert "📚 Categories · 5" in labels
    assert label in labels


def test_home_digest_button_asks_before_sending():
    kb = home_keyboard(_state())
    data = [b.callback_data for row in kb.inline_keyboard for b in row]
    # The button opens a confirmation, never the send itself.
    assert "home_digest" in data
    assert "home_digest_go" not in data


def test_home_status_calls_out_paused_sources():
    """A paused source produces nothing on purpose — but it is exactly the kind of
    thing that gets forgotten, so the header says so once nothing is failing."""
    assert "⏸ 2 paused" in home_text(_state(paused=2))
    assert "⚠️ 1 failing" in home_text(_state(paused=2, errored=1))  # failing still wins

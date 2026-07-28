"""The schedule-string algebra shared by the scheduler and the timetable UI."""
from src.common.schedule import fires_at, format_times, parse_times, slots_by_time, with_time, without_time


def test_parse_times():
    assert parse_times("15:00,21:00") == [(15, 0), (21, 0)]
    assert parse_times("9:30") == [(9, 30)]
    assert parse_times("15:00, ,21:00") == [(15, 0), (21, 0)]
    assert parse_times("garbage") == []
    assert parse_times("") == []


def test_parse_times_rejects_out_of_range():
    # An out-of-range value reaching CronTrigger raises on every job rebuild and at
    # startup, so the parser — the single gate in front of it — drops them here.
    assert parse_times("25:99") == []
    assert parse_times("24:00") == []
    assert parse_times("-1:00") == []
    assert parse_times("11:00,25:00") == [(11, 0)]


def test_format_times_is_canonical():
    # Stored values drifted into mixed spacing/padding; every write normalises now.
    assert format_times([(9, 0), (21, 0), (9, 0)]) == "09:00,21:00"
    assert format_times([]) == ""


def test_with_and_without_time_round_trip():
    assert with_time("11:00,21:00", "16:00") == "11:00,16:00,21:00"
    assert with_time("11:00, 21:00", "21:00") == "11:00,21:00"  # already there, no duplicate
    assert without_time("11:00, 16:00, 21:00", "16:00") == "11:00,21:00"
    assert without_time("9:00", "09:00") == ""  # padding must not hide a match
    assert without_time("11:00,21:00", "16:00") == "11:00,21:00"


def test_fires_at_ignores_spacing_and_zero_padding():
    assert fires_at({"digest_time": "9:00"}, "09:00")
    assert fires_at({"digest_time": "11:00, 16:00"}, "16:00")
    assert not fires_at({"digest_time": "11:00"}, "16:00")
    assert not fires_at({"digest_time": ""}, "11:00")


def test_slots_by_time_groups_and_sorts():
    cats = [
        {"name": "feed", "digest_time": "11:00, 16:00"},
        {"name": "ai", "digest_time": "9:00,11:00"},
    ]
    slots = slots_by_time(cats)
    assert list(slots) == ["09:00", "11:00", "16:00"]
    assert [c["name"] for c in slots["11:00"]] == ["feed", "ai"]

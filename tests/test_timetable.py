"""The timetable screen: slot grouping, quiet-sources marker and rendering."""
from src.bot.keyboards import _timetable_keyboard, _timetable_slots, _timetable_text

CATS = [
    {"name": "world", "emoji": "🌍", "digest_time": "09:00,19:00"},
    {"name": "crypto", "emoji": "🪙", "digest_time": "9:00,14:00"},
    {"name": "ai", "emoji": "🧠", "digest_time": "19:00"},
]


def test_slots_group_by_time_and_sort():
    slots = _timetable_slots(CATS)
    assert list(slots) == ["09:00", "14:00", "19:00"]
    # "9:00" and "09:00" are the same cron slot, so they must land in one row.
    assert [c["name"] for c in slots["09:00"]] == ["world", "crypto"]
    assert [c["name"] for c in slots["19:00"]] == ["world", "ai"]


def test_text_marks_only_the_last_slot_as_quiet():
    text = _timetable_text(CATS)
    lines = [l for l in text.splitlines() if l.startswith("<b>")]
    assert len(lines) == 3
    assert "quiet sources" not in lines[0] and "quiet sources" not in lines[1]
    assert "quiet sources" in lines[2]


def test_text_without_any_time():
    assert "No digest times set." in _timetable_text([])
    assert "No digest times set." in _timetable_text([{"name": "x", "emoji": "📌", "digest_time": "garbage"}])


def test_keyboard_has_a_button_per_category():
    kb = _timetable_keyboard(CATS)
    rows = kb.inline_keyboard
    assert [b[0].callback_data for b in rows] == ["tt_edit:world", "tt_edit:crypto", "tt_edit:ai", "cat_list"]
    assert "09:00,19:00" in rows[0][0].text

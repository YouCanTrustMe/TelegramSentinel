"""The timetable screen: schedule grouping, the per-slot toggle keyboard, and the
guard that stops a category from being left with no digest time at all."""
from src.bot.handlers.timetable import _orphans, _split_toggle_data
from src.bot.keyboards import _slot_keyboard, _slot_text, _timetable_keyboard, _timetable_text
from src.common.schedule import slots_by_time

# Mirrors production: most categories share 11:00/21:00, two also run at 16:00.
CATS = [
    {"name": "feed", "emoji": "📰", "digest_time": "11:00, 16:00, 21:00"},
    {"name": "ai", "emoji": "💠", "digest_time": "11:00,21:00"},
    {"name": "crypto", "emoji": "💸", "digest_time": "11:00,16:00,21:00"},
    {"name": "hrvatska", "emoji": "🇭🇷", "digest_time": "9:00"},
]


def test_slots_group_by_time_and_sort():
    slots = slots_by_time(CATS)
    assert list(slots) == ["09:00", "11:00", "16:00", "21:00"]
    assert [c["name"] for c in slots["16:00"]] == ["feed", "crypto"]


def test_text_groups_categories_sharing_a_schedule():
    text = _timetable_text(CATS)
    # "11:00, 16:00, 21:00" and "11:00,16:00,21:00" are the same schedule despite the
    # spacing, so feed and crypto must collapse into one entry rather than two.
    assert text.count("<b>11:00 · 16:00 · 21:00</b>") == 1
    assert "📰 feed   💸 crypto" in text
    assert "quiet sources ride with the 21:00 digest" in text


def test_text_surfaces_categories_that_are_never_sent():
    text = _timetable_text(CATS + [{"name": "orphan", "emoji": "❓", "digest_time": ""}])
    assert "⚠️ never" in text and "❓ orphan" in text


def test_text_without_any_time():
    assert "No categories yet." in _timetable_text([])
    # Nothing is scheduled, but the categories still exist and must stay visible —
    # this is the state where every one of them is silently never sent.
    broken = _timetable_text([{"name": "x", "emoji": "📌", "digest_time": "garbage"}])
    assert "⚠️ never" in broken and "📌 x" in broken
    assert "quiet sources" not in broken


def test_timetable_keyboard_offers_the_slots_then_add():
    rows = _timetable_keyboard(CATS).inline_keyboard
    assert [b.callback_data for b in rows[0]] == ["tt_slot:09:00", "tt_slot:11:00", "tt_slot:16:00"]
    assert [b.callback_data for b in rows[1]] == ["tt_slot:21:00"]
    assert rows[-2][0].callback_data == "tt_add"
    assert rows[-1][0].callback_data == "cat_list"


def test_slot_keyboard_marks_membership():
    rows = _slot_keyboard("16:00", CATS).inline_keyboard
    marks = {b.callback_data.rsplit(":", 1)[1]: b.text[0] for row in rows for b in row if b.callback_data.startswith("tt_toggle:")}
    assert marks == {"feed": "✅", "crypto": "✅", "ai": "⬜", "hrvatska": "⬜"}
    assert rows[-2][0].callback_data == "tt_del:16:00"
    assert rows[-1][0].callback_data == "tt_list"


def test_slot_keyboard_for_a_brand_new_time_offers_no_remove():
    rows = _slot_keyboard("08:30", CATS).inline_keyboard
    assert all("tt_del" not in (b.callback_data or "") for row in rows for b in row)
    assert "Nothing goes out at 08:30 yet" in _slot_text("08:30", CATS)


def test_toggle_callback_survives_the_colon_in_the_time():
    # Regression: the payload is "tt_toggle:HH:MM:name" and HH:MM has its own colon,
    # so a left-to-right split read the time as "16" and the category as "00:crypto".
    for row in _slot_keyboard("16:00", CATS).inline_keyboard:
        for button in row:
            if button.callback_data.startswith("tt_toggle:"):
                time_str, cat_name = _split_toggle_data(button.callback_data)
                assert time_str == "16:00"
                assert cat_name in {c["name"] for c in CATS}


def test_orphans_names_categories_that_would_lose_their_only_time():
    assert _orphans(CATS, "09:00") == ["hrvatska"]  # hrvatska runs at 09:00 and nowhere else
    assert _orphans(CATS, "16:00") == []

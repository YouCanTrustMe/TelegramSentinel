"""Row and title rendering for the list screens. These are the strings the admin
reads on every visit, and two ship-ready bugs have hidden in handler closures
before, so the decisions live in module-level functions and are checked here."""
from src.bot.keyboards import (
    _blocked_text,
    _categories_text,
    _category_label,
    _clip,
    _filter_label,
    _source_label,
    _times_label,
)


def _cat(name="world", emoji="🌍", digest_time="09:00,14:00,19:00"):
    return {"name": name, "emoji": emoji, "digest_time": digest_time}


def test_clip_cuts_at_a_word_boundary():
    clipped = _clip("Financial Times Companies feed for the day", 20)
    assert clipped.endswith("…")
    assert len(clipped) <= 20
    assert not clipped.startswith("Financial Times C…")


def test_clip_leaves_a_short_label_alone():
    assert _clip("world · 9 · 09:00") == "world · 9 · 09:00"


def test_clip_falls_back_to_a_hard_cut_without_spaces():
    assert _clip("a" * 60, 10) == "a" * 9 + "…"


def test_times_label_sorts_and_dedupes():
    assert _times_label("19:00,09:00,09:00") == "09:00 19:00"


def test_times_label_flags_an_unscheduled_category():
    assert _times_label("") == "⚠️ never"


def test_category_label_carries_count_and_schedule():
    assert _category_label(_cat(), 9) == "🌍 world · 9 · 09:00 14:00 19:00"


def test_category_label_marks_quiet_sources():
    assert "⏸2" in _category_label(_cat(digest_time="09:00"), 9, quiet_count=2)


def test_source_label_icons_by_type_and_status():
    assert _source_label({"name": "Reuters", "type": "rss", "status": "active"}).startswith("🔗")
    assert _source_label({"name": "Suspilne", "type": "telegram", "status": "active"}).startswith("📡")
    assert _source_label({"name": "New one", "type": "telegram", "status": "pending"}).startswith("⏳")


def test_filter_label_keeps_scope_and_hits_when_the_rule_is_long():
    label = _filter_label("crypto price predictions and pump-and-dump posts", 1, 41)
    assert label.endswith(" · 1 cat · 41")
    assert "…" in label


def test_filter_label_says_all_when_unscoped():
    assert _filter_label("sports results", 0, 22) == "sports results · all · 22"


def test_categories_title_counts_sources_and_quiet():
    text = _categories_text([_cat(), _cat("crypto")], 41, quiet_count=2)
    assert "· 2 · 41 sources" in text
    assert "⏸ 2 quiet" in text


def test_empty_screens_say_what_to_do_next():
    assert "➕ adds a category" in _categories_text([], 0)
    assert "crypto price predictions" in _blocked_text([])


def test_blocked_title_shows_the_30d_block_count():
    assert "12 · 106 blocks in 30d" in _blocked_text([{}] * 12, 106)


def test_a_failing_source_is_marked_not_disguised_as_a_healthy_one():
    label = _source_label({"name": "Import AI", "type": "rss", "status": "error"})
    assert label.startswith("⚠️")

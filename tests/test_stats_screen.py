"""Stats renders from a plain state dict. The numbers are plain text now: a
spoiler tap in the admin's own private chat was friction with no payoff."""
from src.bot.stats import stats_text


def _state(**over):
    state = {
        "total": 187, "pending": 34,
        "quiet": [],
        "categories": [{
            "name": "world", "emoji": "🌍", "total": 63, "muted": 0,
            "sources": [
                {"name": "Suspilne", "count": 31, "unsent": 0},
                {"name": "Reuters", "count": 18, "unsent": 3},
            ],
        }],
    }
    state.update(over)
    return state


def test_numbers_are_not_hidden_behind_spoilers():
    text = stats_text(_state())
    assert "<tg-spoiler>" not in text
    assert "187 collected · 34 pending" in text


def test_quiet_sources_lead_the_screen():
    text = stats_text(_state(quiet=[("Import AI", 11), ("Index.hr", 6)]))
    quiet_at = text.index("⏸ Quiet")
    assert quiet_at < text.index("🌍 world")
    assert "Import AI · 11d" in text


def test_category_block_is_expandable_and_carries_pending_marks():
    text = stats_text(_state())
    assert "<blockquote expandable><b>🌍 world</b> · 63" in text
    assert "Reuters <b>18</b> ⏳3" in text
    assert "Suspilne <b>31</b> ·" in text


def test_muted_count_is_shown_only_when_dedup_actually_muted_something():
    assert "muted as dupes" not in stats_text(_state())
    loud = _state()
    loud["categories"][0]["muted"] = 12
    assert "· 12 muted as dupes" in stats_text(loud)


def test_empty_day_says_so_instead_of_rendering_a_bare_header():
    assert "Nothing collected" in stats_text(_state(categories=[], total=0, pending=0))


def test_source_names_are_escaped():
    state = _state()
    state["categories"][0]["sources"][0]["name"] = "A & B <news>"
    assert "A &amp; B &lt;news&gt;" in stats_text(state)


def test_a_source_that_never_produced_reads_as_never():
    assert "Brand new · never" in stats_text(_state(quiet=[("Brand new", None)]))

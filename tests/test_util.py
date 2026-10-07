"""Shared helpers: optional row access and the empty-summary predicate."""
from src.common.util import needs_summary, row_get


def test_row_get_present_and_missing():
    assert row_get({"a": 1}, "a") == 1
    assert row_get({"a": 1}, "b") is None
    assert row_get({"a": 1}, "b", "x") == "x"


def test_row_get_returns_falsy_value_over_default():
    assert row_get({"a": 0}, "a", 99) == 0
    assert row_get({"a": ""}, "a", "d") == ""


def test_needs_summary_true_when_empty_summary_with_raw_text():
    assert needs_summary({"summary": "", "raw_text": "body"}) is True
    assert needs_summary({"summary": "   ", "raw_text": "body"}) is True


def test_needs_summary_false_when_summarised_or_no_raw_text():
    assert needs_summary({"summary": "done", "raw_text": "body"}) is False
    assert needs_summary({"summary": "", "raw_text": ""}) is False
    assert needs_summary({"summary": None, "raw_text": None}) is False


def test_a_channel_signature_is_found_from_its_own_posts():
    from src.common.util import detect_footers
    posts = [f"Story {n}\n\n➕ Subscribe to LIVE\n📸 Send news" for n in range(5)]
    posts += ["One-line post", "Story without a signature\nsecond line"]
    assert detect_footers(posts) == {"📸 Send news", "➕ Subscribe to LIVE"}


def test_a_line_that_ends_few_posts_is_not_a_signature():
    from src.common.util import detect_footers
    assert detect_footers([f"Story {n}\nSame ending" for n in range(4)]) == set()
    # A post that IS the line, alone, does not count towards it.
    assert detect_footers(["Same ending"] * 10) == set()


def test_stripping_a_signature_keeps_the_post_and_never_empties_it():
    from src.common.util import strip_footers
    footers = {"→ Про Бізнес", "📸 Send news"}
    assert strip_footers("У Києві горить ринок «Шайба».\n\n→ Про Бізнес", footers) == "У Києві горить ринок «Шайба»."
    assert strip_footers("[Photo]\n📸 Send news", footers) == "[Photo]"
    assert strip_footers("→ Про Бізнес", footers) == "→ Про Бізнес"
    assert strip_footers("→ Про Бізнес\nreal last line", footers) == "→ Про Бізнес\nreal last line"


def test_a_template_channels_recurring_line_is_news_not_a_signature():
    """An alert channel posting "Threat: drones / Kyiv region" all day: the region line
    ends every post, but the text above it repeats too, so cutting it would lose the news."""
    from src.common.util import detect_footers
    posts = ["🔴 Threat: drones\nKyiv region"] * 8 + ["🟢 All clear\nKyiv region"] * 8
    assert detect_footers(posts) == set()


def test_five_different_alert_types_still_do_not_make_the_region_a_signature():
    """Review 2026-10-07: five distinct template bodies over one region line passed the
    distinct-text check alone."""
    from src.common.util import detect_footers
    alerts = ["🔴 Threat: drones", "🔴 Threat: ballistic", "🟢 All clear", "💥 Explosions", "🔴 Threat: KAB"]
    posts = [f"{alerts[n % 5]}\nKyiv region" for n in range(40)]
    assert detect_footers(posts) == set()

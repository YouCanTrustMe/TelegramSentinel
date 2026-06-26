"""RSS raw-text composition: the headline (title) must not be dropped — some feeds
(e.g. FT) put the real news in the title and only a vague standfirst in the
description. _compose_raw_text combines distinct title+body, else uses whichever
is present without duplicating one inside the other."""
from src.collectors.rss_collector import _compose_raw_text


def test_distinct_title_and_body_are_combined():
    assert _compose_raw_text(
        "Apple raises MacBook prices by 20%",
        "iPhone maker blamed cost rises on memory chip shortages",
    ) == "Apple raises MacBook prices by 20%. iPhone maker blamed cost rises on memory chip shortages"


def test_body_containing_title_uses_body_only():
    # body already includes the headline → no duplication
    assert _compose_raw_text("Oil drops", "Oil drops below $70 a barrel as supply rises") == \
        "Oil drops below $70 a barrel as supply rises"


def test_title_containing_body_uses_title_only():
    assert _compose_raw_text("Full story: bank collapses overnight", "bank collapses") == \
        "Full story: bank collapses overnight"


def test_empty_body_falls_back_to_title():
    assert _compose_raw_text("Headline only", "") == "Headline only"


def test_empty_title_falls_back_to_body():
    assert _compose_raw_text("", "Body only standfirst") == "Body only standfirst"


def test_both_empty_returns_empty():
    assert _compose_raw_text("", "") == ""
    assert _compose_raw_text(None, None) == ""


def test_case_insensitive_containment():
    # title present in body case-insensitively → body only, no duplicate
    assert _compose_raw_text("BREXIT", "What brexit means for trade") == "What brexit means for trade"

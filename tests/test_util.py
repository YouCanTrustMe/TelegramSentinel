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

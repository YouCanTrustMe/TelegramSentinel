"""Groq json_validate_failed recovery: the model occasionally emits unescaped
double quotes inside string values; _escape_stray_quotes / _coerce_json repair it."""
from src.processor.groq_client import _coerce_json, _escape_stray_quotes, _repair_from_error


def test_valid_json_passes_through():
    assert _coerce_json('{"a": 1, "b": "x"}') == {"a": 1, "b": "x"}


def test_non_object_returns_none():
    assert _coerce_json("[1, 2, 3]") is None
    assert _coerce_json("totally broken {") is None


def test_repairs_real_unescaped_inner_quotes():
    # The exact failure mode seen in production logs: a summary with straight
    # quotes around «втілену AI» breaks the JSON.
    bad = (
        '{"items": ['
        '{"id": 1, "summary": "ставку на "втілену AI"", "key_phrase": "Bitcoin"}'
        ']}'
    )
    out = _coerce_json(bad)
    assert out is not None
    assert out["items"][0]["id"] == 1
    assert "втілену AI" in out["items"][0]["summary"]


def test_escape_leaves_clean_json_untouched():
    clean = '{"k": "no inner quotes here"}'
    assert _escape_stray_quotes(clean) == clean


def test_repair_from_error_requires_json_validate_code():
    class FakeExc:
        body = {"error": {"code": "rate_limit", "failed_generation": '{"a": 1}'}}

    assert _repair_from_error(FakeExc()) is None


def test_repair_from_error_recovers_failed_generation():
    class FakeExc:
        body = {
            "error": {
                "code": "json_validate_failed",
                "failed_generation": '{"summary": "a "b" c"}',
            }
        }

    out = _repair_from_error(FakeExc())
    assert out == {"summary": 'a "b" c'}


def test_repair_from_error_handles_missing_body():
    class FakeExc:
        body = None

    assert _repair_from_error(FakeExc()) is None

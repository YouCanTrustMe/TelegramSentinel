"""Classifier pure helpers: Ukrainian detection, prompt-extra keyword switches,
media-prefix stripping and the truncation marker."""
from src.processor.classifier import (
    _looks_ukrainian,
    _mark_big,
    _strip_media_prefix,
    _wants_no_filter,
    _wants_no_merge,
    _wants_no_translate,
)


def test_looks_ukrainian():
    assert _looks_ukrainian("Привіт світ усім людям")
    assert not _looks_ukrainian("Bitcoin price drops today")
    assert _looks_ukrainian("")          # empty -> treated as fine
    assert _looks_ukrainian("Hi")        # <4 letters -> not flagged


def test_proper_nouns_in_ukrainian_still_pass():
    assert _looks_ukrainian("Bitcoin впав на 8% після рішення ФРС")


def test_wants_no_merge():
    assert _wants_no_merge("please no merge here")
    assert _wants_no_merge("не об'єднувати пости")
    assert not _wants_no_merge("focus on numbers")
    assert not _wants_no_merge(None)


def test_wants_no_filter():
    assert _wants_no_filter("bypass filter for this source")
    assert not _wants_no_filter("keep proper nouns")


def test_wants_no_translate():
    assert _wants_no_translate("no translation, keep original language")
    assert not _wants_no_translate(None)


def test_strip_media_prefix():
    assert _strip_media_prefix("[Photo] hello world") == "hello world"
    assert _strip_media_prefix("[Video] clip") == "clip"
    assert _strip_media_prefix("plain text") == "plain text"


def test_mark_big_appends_only_when_truncated():
    long_src = "x" * 2000
    assert _mark_big("summary", long_src, cap=1500).endswith("…")
    assert _mark_big("summary", "short", cap=1500) == "summary"
    assert _mark_big("", long_src, cap=1500) == ""          # empty summary untouched
    assert _mark_big("ends …", long_src, cap=1500) == "ends …"  # no double marker

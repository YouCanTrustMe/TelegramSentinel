"""Classifier pure helpers: Ukrainian detection, prompt-extra keyword switches,
media-prefix stripping and the truncation marker."""
import src.db.models as models
import src.processor.classifier as classifier
import src.processor.cross_dedup as cross_dedup
from src.processor.classifier import (
    ClassificationResult,
    _looks_ukrainian,
    _mark_big,
    _strip_media_prefix,
    _wants_no_filter,
    _wants_no_merge,
    _wants_no_translate,
    classify_pending_items,
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


async def test_classify_pending_embeds_freshly_classified(monkeypatch):
    """Newly summarised items are embedded in the same pass, so the digest finds
    their vectors cached instead of embedding everything at once."""
    long_text = "Реальна новина про важливу подію " * 4  # > _TRIVIAL_MAX_LEN
    items = [
        {"id": 1, "summary": "", "raw_text": long_text},
        {"id": 2, "summary": "", "raw_text": long_text},
    ]
    embedded: dict = {}

    async def fake_get_unsent_items(*a, **k):
        return items

    async def fake_update(item_id, summary, key_phrase):
        pass

    async def fake_classify_batch(payload):
        return {row["id"]: ClassificationResult(summary=f"summary {row['id']}") for row in payload}

    async def fake_ensure_embeddings(passed):
        embedded["ids"] = {row["id"] for row in passed}
        return {}

    monkeypatch.setattr(models, "get_unsent_items", fake_get_unsent_items)
    monkeypatch.setattr(models, "update_item_classification", fake_update)
    monkeypatch.setattr(classifier, "classify_batch", fake_classify_batch)
    monkeypatch.setattr(classifier, "is_quota_dead", lambda *_: False)
    monkeypatch.setattr(classifier.settings, "dedup_enabled", True)
    monkeypatch.setattr(cross_dedup, "ensure_embeddings", fake_ensure_embeddings)

    await classify_pending_items(limit=10)

    assert embedded["ids"] == {1, 2}

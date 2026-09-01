"""Embedding helpers: cosine, blob (de)serialization and the Gemini retryDelay
parser used to time the 429 retry."""
import logging

import numpy as np
import pytest

from src.processor.dedup import embedder
from src.processor.dedup.embedder import _RETRY_DELAY_RE, cosine, from_blob, to_blob


def test_cosine_identical_is_one():
    v = np.array([1.0, 2.0, 3.0], dtype=np.float32)
    assert cosine(v, v) == pytest.approx(1.0)  # float32 -> ~0.99999994


def test_cosine_orthogonal_is_zero():
    assert cosine(np.array([1.0, 0.0]), np.array([0.0, 1.0])) == 0.0


def test_cosine_zero_vector_is_zero():
    assert cosine(np.array([0.0, 0.0]), np.array([1.0, 1.0])) == 0.0


def test_blob_roundtrip():
    vec = [float(i) / 8 for i in range(embedder.active_dims())]
    out = from_blob(to_blob(vec))
    assert out.dtype == np.float32
    assert list(out) == vec


def test_blob_of_another_models_dimension_reads_as_missing():
    # A stored vector from a previous embedding model is not comparable with the
    # active one; reading it as absent makes the item re-embed instead.
    assert from_blob(to_blob([1.5, -2.0, 3.25])) is None


def test_cosine_of_mismatched_dimensions_is_zero_not_an_error():
    # Raising here would abort the whole (fail-open) dedup pass for a digest.
    assert cosine(np.array([1.0, 2.0]), np.array([1.0, 2.0, 3.0])) == 0.0


def test_from_blob_none():
    assert from_blob(None) is None
    assert from_blob(b"") is None


def test_retry_delay_regex():
    m = _RETRY_DELAY_RE.search('{"error": {"details": [{"retryDelay": "53s"}]}}')
    assert m and m.group(1) == "53"
    assert _RETRY_DELAY_RE.search('{"no": "delay"}') is None


def test_single_failed_pass_stays_quiet(caplog):
    embedder._reset_failure_state()
    with caplog.at_level(logging.WARNING, logger="src.processor.dedup.embedder"):
        embedder._note_pass(0, 3)
    assert caplog.records == []


def test_sustained_failure_warns_once_per_outage(caplog):
    embedder._reset_failure_state()
    with caplog.at_level(logging.WARNING, logger="src.processor.dedup.embedder"):
        for _ in range(6):
            embedder._note_pass(0, 3)
    assert len(caplog.records) == 1
    assert "unavailable" in caplog.records[0].getMessage()


def test_recovery_warns_and_rearms_the_outage_alert(caplog):
    embedder._reset_failure_state()
    with caplog.at_level(logging.WARNING, logger="src.processor.dedup.embedder"):
        for _ in range(4):
            embedder._note_pass(0, 3)
        embedder._note_pass(3, 3)
        for _ in range(4):
            embedder._note_pass(0, 3)
    messages = [r.getMessage() for r in caplog.records]
    assert len(messages) == 3
    assert "unavailable" in messages[0] and "recovered" in messages[1] and "unavailable" in messages[2]


def test_success_after_a_short_failure_run_is_silent(caplog):
    embedder._reset_failure_state()
    with caplog.at_level(logging.WARNING, logger="src.processor.dedup.embedder"):
        embedder._note_pass(0, 3)
        embedder._note_pass(3, 3)
    assert caplog.records == []


def test_rate_limited_exception_carries_a_readable_message():
    # The old bare exception rendered as "None" in the log line.
    assert "429" in str(embedder._RateLimited(50.0))
    assert str(embedder._RateLimited(None)) != "None"


def test_mistral_request_shape(monkeypatch):
    monkeypatch.setattr(embedder.settings, "embed_provider", "mistral")
    monkeypatch.setattr(embedder.settings, "embed_model", "mistral-embed")
    monkeypatch.setattr(embedder.settings, "mistral_api_key", "k")
    url, params, headers, body = embedder._request(["a", "b"])
    assert url.endswith("/v1/embeddings") and params == {}
    assert headers["Authorization"] == "Bearer k"
    assert body == {"model": "mistral-embed", "input": ["a", "b"]}


def test_gemini_request_shape(monkeypatch):
    monkeypatch.setattr(embedder.settings, "embed_provider", "gemini")
    monkeypatch.setattr(embedder.settings, "gemini_api_key", "g")
    url, params, headers, body = embedder._request(["a"])
    assert "batchEmbedContents" in url and params == {"key": "g"}
    assert body["requests"][0]["outputDimensionality"] == embedder._GEMINI_DIMS


def test_mistral_response_is_read_in_input_order(monkeypatch):
    monkeypatch.setattr(embedder.settings, "embed_provider", "mistral")
    data = {"data": [{"index": 1, "embedding": [2.0]}, {"index": 0, "embedding": [1.0]}]}
    assert embedder._parse(data, 2) == [[1.0], [2.0]]


def test_short_provider_response_yields_no_vectors(monkeypatch):
    monkeypatch.setattr(embedder.settings, "embed_provider", "mistral")
    assert embedder._parse({"data": [{"index": 0, "embedding": [1.0]}]}, 2) == [None, None]


def test_empty_text_is_never_sent_as_empty_string(monkeypatch):
    # Mistral rejects an empty input string; the caller may hand us one.
    monkeypatch.setattr(embedder.settings, "embed_provider", "mistral")
    _, _, _, body = embedder._request(["", "x"])
    assert body["input"] == [" ", "x"]


def test_dimension_is_taken_from_what_the_provider_returned(monkeypatch):
    # EMBED_MODEL is configurable: a model missing from the table would otherwise
    # have every vector it produced discarded by from_blob straight after storing.
    monkeypatch.setattr(embedder.settings, "embed_provider", "mistral")
    monkeypatch.setattr(embedder.settings, "embed_model", "some-new-embed")
    monkeypatch.setattr(embedder, "_observed_dims", {})
    assert embedder.active_dims() == 1024
    embedder._record_dims([[0.0] * 384, [1.0] * 384])
    assert embedder.active_dims() == 384
    assert from_blob(to_blob([0.5] * 384)) is not None


def test_mixed_length_response_does_not_change_the_dimension(monkeypatch):
    monkeypatch.setattr(embedder.settings, "embed_provider", "mistral")
    monkeypatch.setattr(embedder.settings, "embed_model", "mistral-embed")
    monkeypatch.setattr(embedder, "_observed_dims", {})
    embedder._record_dims([[0.0] * 512, [1.0] * 8])
    assert embedder.active_dims() == 1024


def test_missing_key_counts_toward_the_outage_alert(caplog):
    embedder._reset_failure_state()
    with caplog.at_level(logging.WARNING, logger="src.processor.dedup.embedder"):
        for _ in range(4):
            embedder._note_pass(0, 2)
    assert len(caplog.records) == 1

"""Embedding helpers: cosine, blob (de)serialization and the Gemini retryDelay
parser used to time the 429 retry."""
import numpy as np
import pytest

from src.processor.embedder import _RETRY_DELAY_RE, cosine, from_blob, to_blob


def test_cosine_identical_is_one():
    v = np.array([1.0, 2.0, 3.0], dtype=np.float32)
    assert cosine(v, v) == pytest.approx(1.0)  # float32 -> ~0.99999994


def test_cosine_orthogonal_is_zero():
    assert cosine(np.array([1.0, 0.0]), np.array([0.0, 1.0])) == 0.0


def test_cosine_zero_vector_is_zero():
    assert cosine(np.array([0.0, 0.0]), np.array([1.0, 1.0])) == 0.0


def test_blob_roundtrip():
    out = from_blob(to_blob([1.5, -2.0, 3.25]))
    assert out.dtype == np.float32
    assert list(out) == [1.5, -2.0, 3.25]


def test_from_blob_none():
    assert from_blob(None) is None
    assert from_blob(b"") is None


def test_retry_delay_regex():
    m = _RETRY_DELAY_RE.search('{"error": {"details": [{"retryDelay": "53s"}]}}')
    assert m and m.group(1) == "53"
    assert _RETRY_DELAY_RE.search('{"no": "delay"}') is None

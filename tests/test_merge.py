"""Within-source merge robustness: a misbehaving group_by_topic (empty or
out-of-range `ids`) must never crash the digest. Regression for the
`max() iterable argument is empty` ValueError that killed two prod digests
on 2026-06-29 when the LLM returned a group with empty ids."""
import numpy as np

import src.processor.dedup.merge as mg
from src.processor.dedup.merge import (
    _cluster_summary_fields,
    _llm_subgroup,
    merge_source_items,
)


def _vec(*xy):
    return np.array(xy, dtype=np.float32)


def _item(i, summary="news", url=None):
    return {
        "id": i,
        "summary": summary,
        "key_phrase": "kp",
        "original_url": url or f"https://example.com/{i}",
        "published_at": i,
        "raw_text": summary,
    }


def test_cluster_summary_fields_handles_empty_cluster():
    assert _cluster_summary_fields([]) == ("", "")


async def test_llm_subgroup_drops_empty_and_out_of_range_groups(monkeypatch):
    cluster = [_item(10), _item(11)]

    async def fake_group_by_topic(inputs, prompt_extra=None):
        # An empty group, an out-of-range index, and one valid group.
        return [
            {"ids": [], "summary": "", "key_phrase": ""},
            {"ids": [5], "summary": "phantom", "key_phrase": ""},
            {"ids": [0, 1], "summary": "merged", "key_phrase": "kp"},
        ]

    monkeypatch.setattr(mg, "group_by_topic", fake_group_by_topic)
    out = await _llm_subgroup(cluster, None)
    assert len(out) == 1
    sub, summ, _ = out[0]
    assert [it["id"] for it in sub] == [10, 11]
    assert summ == "merged"


async def test_merge_via_embeddings_survives_empty_llm_group(monkeypatch):
    # Two near (cosine 0.9 -> clusters, below 0.95 near-dup so the LLM is consulted)
    # plus an orthogonal singleton.
    items = [_item(1), _item(2), _item(3)]
    vectors = {1: _vec(1.0, 0.0), 2: _vec(0.9, 0.4359), 3: _vec(0.0, 1.0)}

    async def fake_group_by_topic(inputs, prompt_extra=None):
        # The crash trigger: a group with empty ids alongside a real one.
        return [
            {"ids": [], "summary": "", "key_phrase": ""},
            {"ids": [0, 1], "summary": "", "key_phrase": ""},
        ]

    monkeypatch.setattr(mg, "group_by_topic", fake_group_by_topic)
    monkeypatch.setattr(mg.settings, "merge_via_embeddings", True)

    out = await merge_source_items(items, prompt_extra=None, vectors=vectors)
    # No crash; every input item is still represented exactly once.
    covered = sorted(i for entry in out for i in entry["_item_ids"])
    assert covered == [1, 2, 3]

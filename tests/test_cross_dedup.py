"""Cross-source dedup internals: union-find clustering, placeholder exclusion,
and the B1 LLM-confirmation gate that keeps a bare cosine threshold from hiding
distinct cross-source stories that merely share vocabulary."""
import math

import numpy as np

import src.processor.dedup.cross_dedup as cd
from src.processor.dedup.cross_dedup import _UnionFind, _is_placeholder, cluster_within_source


def test_union_find_groups_transitively():
    uf = _UnionFind()
    uf.union(1, 2)
    uf.union(2, 3)
    assert uf.find(1) == uf.find(3)
    assert uf.find(1) != uf.find(4)


def test_is_placeholder():
    assert _is_placeholder("no text")
    assert _is_placeholder("NO CAPTION")
    assert _is_placeholder("[Photo]")
    assert not _is_placeholder("Real news about something")
    # Known gap: an emoji-only caption is not caught (ends with the emoji, not "]").
    assert not _is_placeholder("[Photo] 🐒")


def _vec(*xy):
    return np.array(xy, dtype=np.float32)


def test_cluster_within_source_groups_by_cosine():
    items = [{"id": 1}, {"id": 2}, {"id": 3}]
    vectors = {1: _vec(1, 0), 2: _vec(1, 0.001), 3: _vec(0, 1)}  # 1≈2, 3 orthogonal
    clusters = cluster_within_source(items, vectors, threshold=0.9)
    sizes = sorted(len(c) for c in clusters)
    assert sizes == [1, 2]


def test_cluster_within_source_keeps_unvectored_as_singletons():
    items = [{"id": 1}, {"id": 2}]
    clusters = cluster_within_source(items, {1: _vec(1, 0)}, threshold=0.9)
    assert sorted(len(c) for c in clusters) == [1, 1]


async def test_confirm_mutes_keeps_llm_rejected_and_auto_confirms_near_dup(monkeypatch):
    calls = {"n": 0}

    async def fake_group_by_topic(inputs, prompt_extra=None):
        calls["n"] += 1
        primary = inputs[0]["id"]
        same = [i["id"] for i in inputs if i["id"] == primary or "SAME" in i["text"]]
        groups = [{"ids": same, "summary": "x", "key_phrase": ""}]
        for i in inputs:
            if i["id"] not in same:
                groups.append({"ids": [i["id"]], "summary": "y", "key_phrase": ""})
        return groups

    monkeypatch.setattr(cd, "group_by_topic", fake_group_by_topic)

    item_by_id = {
        1: {"id": 1, "summary": "primary"},
        2: {"id": 2, "summary": "SAME event"},
        3: {"id": 3, "summary": "DIFFERENT strike"},
        4: {"id": 4, "summary": "near identical repost"},
    }
    ang = math.radians(28)
    vec = {
        1: _vec(1, 0),
        2: _vec(math.cos(ang), math.sin(ang)),  # ~0.88, needs LLM
        3: _vec(math.cos(ang), math.sin(ang)),  # ~0.88, needs LLM
        4: _vec(0.999, 0.001),                  # ~1.0, auto-confirm, no LLM
    }
    muted = {2: 1, 3: 1, 4: 1}

    confirmed = await cd._confirm_mutes(muted, item_by_id, {}, vec, {})

    assert 2 in confirmed          # LLM says same event -> muted
    assert 3 not in confirmed      # LLM says different -> kept
    assert 4 in confirmed          # near-dup auto-confirmed
    assert calls["n"] == 1         # LLM called once; near-dup skipped it


async def test_confirm_band_pair_reaches_llm_and_mutes(monkeypatch):
    """A cross-source pair in the confirm band (dedup_log_floor <= cos < dedup_threshold)
    must be unioned and LLM-confirmed, not silently dropped — Ukrainian war-news
    rephrasings of one event routinely sit at 0.80-0.86."""
    marked: list[tuple[int, int]] = []

    async def fake_recent(_hours):
        return []

    async def fake_mark(mid, pid):
        marked.append((mid, pid))

    async def fake_links(_ids):
        return {}

    async def fake_group_by_topic(inputs, prompt_extra=None):
        return [{"ids": [i["id"] for i in inputs], "summary": "x", "key_phrase": ""}]

    monkeypatch.setattr(cd, "get_recent_embedded_items", fake_recent)
    monkeypatch.setattr(cd, "mark_duplicate", fake_mark)
    monkeypatch.setattr(cd, "get_duplicate_links", fake_links)
    monkeypatch.setattr(cd, "group_by_topic", fake_group_by_topic)
    monkeypatch.setattr(cd.settings, "dedup_shadow", False)

    ang = math.radians(34)  # cosine ~0.829, inside the 0.80-0.86 band
    items = [
        {"id": 1, "summary": "Khmelnytskyi air raid downed 5 drones", "category": "feed",
         "source_id": 10, "source_name": "A", "source_sort_order": 0, "published_at": "1"},
        {"id": 2, "summary": "Khmelnytskyi alert system triggered", "category": "feed",
         "source_id": 11, "source_name": "B", "source_sort_order": 1, "published_at": "2"},
    ]
    vec = {1: _vec(1, 0), 2: _vec(math.cos(ang), math.sin(ang))}

    survivors, _ = await cd.deduplicate(items, vec)

    assert marked == [(2, 1)]                       # band pair muted under primary
    assert [it["id"] for it in survivors] == [1]


async def test_confirm_mutes_chunks_large_groups(monkeypatch):
    """A big candidate group is split so no single group_by_topic call exceeds the
    cap (primary + a few candidates), guarding against the LLM over-grouping."""
    monkeypatch.setattr(cd, "_B1_MAX_GROUP", 3)  # chunk_size = 2 candidates per call
    calls = {"n": 0, "sizes": []}

    async def fake_group_by_topic(inputs, prompt_extra=None):
        calls["n"] += 1
        calls["sizes"].append(len(inputs))
        return [{"ids": [i["id"] for i in inputs], "summary": "x", "key_phrase": ""}]

    monkeypatch.setattr(cd, "group_by_topic", fake_group_by_topic)
    # Primary (id=1) has no vector, so no candidate is auto-confirmed — all need the LLM.
    item_by_id = {i: {"id": i, "summary": f"s{i}"} for i in range(1, 6)}
    vec = {i: _vec(1, 0) for i in range(2, 6)}

    confirmed = await cd._confirm_mutes({2: 1, 3: 1, 4: 1, 5: 1}, item_by_id, {}, vec, {})

    assert calls["n"] == 2               # 4 candidates / 2 per call
    assert max(calls["sizes"]) <= 3      # primary + at most 2 candidates each
    assert all(d in confirmed for d in (2, 3, 4, 5))


async def test_confirm_mutes_fails_open_on_llm_error(monkeypatch):
    async def boom(inputs, prompt_extra=None):
        raise RuntimeError("quota dead")

    monkeypatch.setattr(cd, "group_by_topic", boom)
    item_by_id = {1: {"id": 1, "summary": "p"}, 2: {"id": 2, "summary": "d"}}
    vec = {1: _vec(1, 0), 2: _vec(math.cos(math.radians(28)), math.sin(math.radians(28)))}

    confirmed = await cd._confirm_mutes({2: 1}, item_by_id, {}, vec, {})

    assert confirmed == {}  # LLM error -> nothing muted (no real story hidden)

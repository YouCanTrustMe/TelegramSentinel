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
    rephrasings of one event routinely sit just above the union floor."""
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

    ang = math.radians(28)  # cosine ~0.883, inside the 0.86-0.92 confirm band
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


async def test_near_identical_pair_muted_without_llm(monkeypatch):
    """A cross-source pair at >= merge_near_dup_threshold is a certain repost and must
    be muted directly, WITHOUT the LLM — even when the LLM would reject the link. Such a
    pair can transitively union onto a weakly-related already-sent primary, and the
    confirm-vs-primary step alone then misses it (observed: 0.99-cosine reposts left in)."""
    marked: list[tuple[int, int]] = []
    calls = {"n": 0}

    async def fake_recent(_hours):
        return []

    async def fake_mark(mid, pid):
        marked.append((mid, pid))

    async def fake_links(_ids):
        return {}

    async def fake_group_by_topic(inputs, prompt_extra=None):
        calls["n"] += 1  # would call every candidate a DIFFERENT event
        return [{"ids": [i["id"]], "summary": "y", "key_phrase": ""} for i in inputs]

    monkeypatch.setattr(cd, "get_recent_embedded_items", fake_recent)
    monkeypatch.setattr(cd, "mark_duplicate", fake_mark)
    monkeypatch.setattr(cd, "get_duplicate_links", fake_links)
    monkeypatch.setattr(cd, "group_by_topic", fake_group_by_topic)
    monkeypatch.setattr(cd.settings, "dedup_shadow", False)

    ang = math.radians(5)  # cosine ~0.996, above merge_near_dup_threshold (0.95)
    items = [
        {"id": 1, "summary": "Long-range strike command created", "category": "feed",
         "source_id": 10, "source_name": "A", "source_sort_order": 0, "published_at": "1"},
        {"id": 2, "summary": "Long-range strike command set up", "category": "feed",
         "source_id": 11, "source_name": "B", "source_sort_order": 1, "published_at": "2"},
    ]
    vec = {1: _vec(1, 0), 2: _vec(math.cos(ang), math.sin(ang))}

    survivors, _ = await cd.deduplicate(items, vec)

    assert marked == [(2, 1)]                       # lower-priority repost muted
    assert [it["id"] for it in survivors] == [1]
    assert calls["n"] == 0                           # near-identical -> LLM never consulted


async def test_near_dup_chain_collapses_to_surviving_primary(monkeypatch):
    """A near-dup primary (X) can itself be muted under a higher-priority floor match (Y):
    Z->X and X->Y. The chain must collapse so Z points at the SURVIVOR Y, not the hidden X
    (otherwise Z's source link would render under a story that isn't shown)."""
    marked: list[tuple[int, int]] = []

    async def fake_recent(_hours):
        return []

    async def fake_mark(mid, pid):
        marked.append((mid, pid))

    async def fake_links(_ids):
        return {}

    async def fake_group_by_topic(inputs, prompt_extra=None):
        return [{"ids": [i["id"] for i in inputs], "summary": "x", "key_phrase": ""}]  # all same event

    monkeypatch.setattr(cd, "get_recent_embedded_items", fake_recent)
    monkeypatch.setattr(cd, "mark_duplicate", fake_mark)
    monkeypatch.setattr(cd, "get_duplicate_links", fake_links)
    monkeypatch.setattr(cd, "group_by_topic", fake_group_by_topic)
    monkeypatch.setattr(cd.settings, "dedup_shadow", False)

    a = math.radians(3)   # X(id=2) & Z(id=3): cosine ~0.9986 -> near-dup, primary X
    y = math.radians(27)  # X(id=2) & Y(id=1): cosine ~0.891 -> floor band, Y wins on priority
    items = [
        {"id": 1, "summary": "strike variant", "category": "feed",
         "source_id": 30, "source_name": "Y", "source_sort_order": 0, "published_at": "1"},
        {"id": 2, "summary": "strike A", "category": "feed",
         "source_id": 31, "source_name": "X", "source_sort_order": 2, "published_at": "2"},
        {"id": 3, "summary": "strike A repost", "category": "feed",
         "source_id": 32, "source_name": "Z", "source_sort_order": 3, "published_at": "3"},
    ]
    vec = {1: _vec(math.cos(y), math.sin(y)), 2: _vec(1, 0), 3: _vec(math.cos(a), math.sin(a))}

    survivors, _ = await cd.deduplicate(items, vec)

    assert [it["id"] for it in survivors] == [1]          # only Y survives
    assert sorted(marked) == [(2, 1), (3, 1)]             # both point at Y, not Z->X->hidden


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


async def test_confirm_mutes_batches_many_primaries_into_one_call(monkeypatch):
    """Several small primaries are packed into a single group_by_topic call (Cerebras
    is 5 RPM, so one call per primary throttles a big digest). Each candidate is still
    confirmed only if the LLM groups it with ITS OWN primary."""
    calls = {"n": 0, "sizes": []}

    async def fake_group_by_topic(inputs, prompt_extra=None):
        calls["n"] += 1
        calls["sizes"].append(len(inputs))
        # Group each primary (odd id) with the very next id ("its" candidate), leaving
        # the second candidate of primary 1 (id=3) as a separate event -> kept.
        pairs = {1: {1, 2}, 5: {5, 6}, 7: {7, 8}}
        present = {i["id"] for i in inputs}
        groups = []
        seen: set[int] = set()
        for p, members in pairs.items():
            m = members & present
            if m:
                groups.append({"ids": list(m), "summary": "x", "key_phrase": ""})
                seen |= m
        for i in inputs:
            if i["id"] not in seen:
                groups.append({"ids": [i["id"]], "summary": "y", "key_phrase": ""})
        return groups

    monkeypatch.setattr(cd, "group_by_topic", fake_group_by_topic)
    # 3 primaries (1,5,7); primary 1 has two candidates (2 SAME, 3 different); 5 and 7 have one each.
    item_by_id = {i: {"id": i, "summary": f"s{i}"} for i in (1, 2, 3, 5, 6, 7, 8)}
    # No primary has a vector -> nothing auto-confirmed as near-dup, all go to the LLM.
    vec = {i: _vec(1, 0) for i in (2, 3, 6, 8)}
    muted = {2: 1, 3: 1, 6: 5, 8: 7}

    confirmed = await cd._confirm_mutes(muted, item_by_id, {}, vec, {})

    assert calls["n"] == 1                 # all three primaries fit one batch (<= _B1_CONFIRM_BATCH)
    assert confirmed == {2: 1, 6: 5, 8: 7}  # each candidate muted under its OWN primary
    assert 3 not in confirmed              # LLM kept it as a different event


async def test_confirm_mutes_fails_open_on_llm_error(monkeypatch):
    async def boom(inputs, prompt_extra=None):
        raise RuntimeError("quota dead")

    monkeypatch.setattr(cd, "group_by_topic", boom)
    item_by_id = {1: {"id": 1, "summary": "p"}, 2: {"id": 2, "summary": "d"}}
    vec = {1: _vec(1, 0), 2: _vec(math.cos(math.radians(28)), math.sin(math.radians(28)))}

    confirmed = await cd._confirm_mutes({2: 1}, item_by_id, {}, vec, {})

    assert confirmed == {}  # LLM error -> nothing muted (no real story hidden)


def _band_vec(deg):
    a = math.radians(deg)
    return _vec(math.cos(a), math.sin(a))


async def test_confirm_mutes_regroups_candidates_rejected_against_a_weak_primary(monkeypatch):
    """Prod 2026-09-02: two sources reported one downed Ka-27 (cosine 0.966) and both
    were delivered — union-find had chained them onto an unrelated primary, and the
    confirm step only asks "same event as the PRIMARY?". The LLM's own partition puts
    the two together, so the pair must collapse without a second call."""
    calls = {"n": 0}

    async def fake_group_by_topic(inputs, prompt_extra=None):
        calls["n"] += 1
        ids = {i["id"] for i in inputs}
        groups = [{"ids": [1], "summary": "weak anchor", "key_phrase": ""}]
        groups.append({"ids": sorted(ids - {1}), "summary": "one event", "key_phrase": ""})
        return groups

    monkeypatch.setattr(cd, "group_by_topic", fake_group_by_topic)

    item_by_id = {
        1: {"id": 1, "summary": "anchor", "source_id": 10, "source_sort_order": 0},
        2: {"id": 2, "summary": "Ka-27 destroyed", "source_id": 11, "source_sort_order": 1},
        3: {"id": 3, "summary": "destruction of a Ka-27 confirmed", "source_id": 12, "source_sort_order": 2},
    }
    vec = {1: _band_vec(0), 2: _band_vec(28), 3: _band_vec(29)}  # 2~3 ≈ 1.0, both ~0.88 to 1

    confirmed = await cd._confirm_mutes({2: 1, 3: 1}, item_by_id, {}, vec, {})

    assert calls["n"] == 1
    assert 2 not in confirmed              # lowest sort_order survives
    assert confirmed[3] == 2               # the other is muted under it, not under the anchor


async def test_regroup_requires_the_pair_to_clear_the_cosine_floor(monkeypatch):
    """Embeddings stay the gate: an LLM that lumps two candidates together cannot mute
    a pair whose own vectors never linked them."""
    item_by_id = {
        2: {"id": 2, "summary": "a", "source_id": 11, "source_sort_order": 1},
        3: {"id": 3, "summary": "b", "source_id": 12, "source_sort_order": 2},
    }
    vec = {2: _band_vec(0), 3: _band_vec(60)}  # cosine 0.5, far below the floor

    out = cd._regroup_rejected([(2, 1), (3, 1)], [{2, 3}], item_by_id, vec)

    assert out == {}


async def test_regroup_leaves_same_source_pairs_to_the_within_source_merge():
    item_by_id = {
        2: {"id": 2, "summary": "a", "source_id": 11, "source_sort_order": 1},
        3: {"id": 3, "summary": "b", "source_id": 11, "source_sort_order": 1},
    }
    vec = {2: _band_vec(0), 3: _band_vec(1)}

    assert cd._regroup_rejected([(2, 1), (3, 1)], [{2, 3}], item_by_id, vec) == {}


def test_regroup_never_crosses_categories():
    """One confirm call packs primaries from several categories, so its partition can
    hold a group spanning them. Muting across a category boundary would render the
    duplicate's link under a primary the reader meets in a different section."""
    item_by_id = {
        2: {"id": 2, "summary": "a", "source_id": 11, "source_sort_order": 1, "category": "crypto"},
        3: {"id": 3, "summary": "b", "source_id": 12, "source_sort_order": 2, "category": "finance"},
    }
    vec = {2: _band_vec(0), 3: _band_vec(1)}  # cosine ~1.0, would otherwise collapse

    assert cd._regroup_rejected([(2, 1), (3, 1)], [{2, 3}], item_by_id, vec) == {}

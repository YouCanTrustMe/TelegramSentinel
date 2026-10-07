"""Cross-source dedup internals: union-find clustering, placeholder exclusion,
and the B1 LLM-confirmation gate that keeps a bare cosine threshold from hiding
distinct cross-source stories that merely share vocabulary."""
import math

import pytest

import numpy as np

import src.processor.dedup.cross_dedup as cd
from src.processor.dedup.cross_dedup import _UnionFind, _is_placeholder, cluster_within_source
from src.processor.dedup.embedder import to_blob


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


def _judge(rule):
    """A judge_pairs stub: rule(text_a, text_b) -> verdict; counts calls and pairs."""
    calls = {"n": 0, "pairs": []}

    async def judge(pairs):
        calls["n"] += 1
        calls["pairs"] += list(pairs)
        return [rule(a, b) for a, b in pairs]

    return judge, calls


async def test_confirm_mutes_judges_each_pair_and_auto_hides_close_ones(monkeypatch):
    judge, calls = _judge(lambda a, b: "same" if "SAME" in b else "different")
    monkeypatch.setattr(cd, "judge_pairs", judge)

    item_by_id = {
        1: {"id": 1, "raw_text": "primary"},
        2: {"id": 2, "raw_text": "SAME event"},
        3: {"id": 3, "raw_text": "DIFFERENT strike"},
        4: {"id": 4, "raw_text": "close rewrite"},
    }
    ang = math.radians(28)
    vec = {
        1: _vec(1, 0),
        2: _vec(math.cos(ang), math.sin(ang)),  # ~0.88, judged
        3: _vec(math.cos(ang), math.sin(ang)),  # ~0.88, judged
        4: _at(0.95),                           # >= dedup_auto_hide_threshold, no LLM
    }

    confirmed, updates = await cd._confirm_mutes({2: 1, 3: 1, 4: 1}, item_by_id, {}, vec, {})

    assert confirmed == {2: 1, 4: 1}
    assert updates == {}
    assert [b for _a, b in calls["pairs"]] == ["SAME event", "DIFFERENT strike"]


async def test_confirm_mutes_reads_the_raw_post_not_the_summary(monkeypatch):
    """A one-line summary drops exactly the new fact that makes a post news."""
    judge, calls = _judge(lambda a, b: "different")
    monkeypatch.setattr(cd, "judge_pairs", judge)
    item_by_id = {2: {"id": 2, "summary": "short", "raw_text": "the whole post"}}
    vec = {2: _at(0.88)}

    await cd._confirm_mutes({2: 99}, item_by_id, {99: "the shown post"}, vec, {99: _vec(1, 0)})

    assert calls["pairs"] == [("the shown post", "the whole post")]


async def test_an_update_hides_inside_one_digest_but_not_against_a_shown_story(monkeypatch):
    """In one digest the primary is the richer post, so the update adds nothing the
    reader misses. Against a story shown earlier the new fact IS the news."""
    judge, _ = _judge(lambda a, b: "update")
    monkeypatch.setattr(cd, "judge_pairs", judge)
    item_by_id = {1: {"id": 1, "raw_text": "p"}, 2: {"id": 2, "raw_text": "d"}, 3: {"id": 3, "raw_text": "e"}}
    vec = {1: _vec(1, 0), 2: _at(0.88), 3: _at(0.88)}

    confirmed, updates = await cd._confirm_mutes({2: 1, 3: 99}, item_by_id, {99: "shown"}, vec, {99: _vec(1, 0)})

    assert confirmed == {2: 1}
    assert updates == {3: 99}


async def test_confirm_mutes_fails_open_on_a_missing_verdict(monkeypatch):
    judge, _ = _judge(lambda a, b: None)
    monkeypatch.setattr(cd, "judge_pairs", judge)
    item_by_id = {1: {"id": 1, "raw_text": "p"}, 2: {"id": 2, "raw_text": "d"}}
    vec = {1: _vec(1, 0), 2: _at(0.88)}

    assert await cd._confirm_mutes({2: 1}, item_by_id, {}, vec, {}) == ({}, {})


async def test_confirm_band_pair_reaches_llm_and_mutes(monkeypatch):
    """A cross-source pair in the confirm band (dedup_log_floor <= cos < dedup_threshold)
    must be unioned and LLM-confirmed, not silently dropped — Ukrainian war-news
    rephrasings of one event routinely sit just above the union floor."""
    marked = _wire(monkeypatch)
    items = [
        {"id": 1, "summary": "Khmelnytskyi air raid downed 5 drones", "category": "feed",
         "source_id": 10, "source_name": "A", "source_sort_order": 0, "published_at": "1"},
        {"id": 2, "summary": "Khmelnytskyi alert system triggered", "category": "feed",
         "source_id": 11, "source_name": "B", "source_sort_order": 1, "published_at": "2"},
    ]
    vec = {1: _vec(1, 0), 2: _at(0.883)}

    survivors, _ = await cd.deduplicate(items, vec)

    assert marked == [(2, 1)]
    assert [it["id"] for it in survivors] == [1]


async def test_near_identical_pair_muted_without_llm(monkeypatch):
    """A cross-source pair at >= merge_near_dup_threshold is a certain repost and must
    be muted directly, WITHOUT the LLM — even when the LLM would reject the link. Such a
    pair can transitively union onto a weakly-related already-sent primary, and the
    confirm-vs-primary step alone then misses it (observed: 0.99-cosine reposts left in)."""
    marked = _wire(monkeypatch, same_event=False)
    judge, calls = _judge(lambda a, b: "different")
    monkeypatch.setattr(cd, "judge_pairs", judge)
    items = [
        {"id": 1, "summary": "Long-range strike command created", "category": "feed",
         "source_id": 10, "source_name": "A", "source_sort_order": 0, "published_at": "1"},
        {"id": 2, "summary": "Long-range strike command set up", "category": "feed",
         "source_id": 11, "source_name": "B", "source_sort_order": 1, "published_at": "2"},
    ]
    vec = {1: _vec(1, 0), 2: _at(0.996)}

    survivors, _ = await cd.deduplicate(items, vec)

    assert marked == [(2, 1)]
    assert [it["id"] for it in survivors] == [1]
    assert calls["pairs"] == []


async def test_near_dup_chain_collapses_to_surviving_primary(monkeypatch):
    """A near-dup primary (X) can itself be muted under a higher-priority floor match (Y):
    Z->X and X->Y. The chain must collapse so Z points at the SURVIVOR Y, not the hidden X
    (otherwise Z's source link would render under a story that isn't shown)."""
    marked = _wire(monkeypatch)
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

    assert [it["id"] for it in survivors] == [1]
    assert sorted(marked) == [(2, 1), (3, 1)]


def _band_vec(deg):
    a = math.radians(deg)
    return _vec(math.cos(a), math.sin(a))


async def test_confirm_mutes_regroups_candidates_rejected_against_a_weak_primary(monkeypatch):
    """Prod 2026-09-02: two sources reported one downed Ka-27 (cosine 0.966) and both
    were delivered — union-find had chained them onto an unrelated primary, and the
    confirm step only asks "same event as the PRIMARY?". The two must still collapse."""
    judge, calls = _judge(lambda a, b: "different" if a == "anchor" else "same")
    monkeypatch.setattr(cd, "judge_pairs", judge)
    item_by_id = {
        1: {"id": 1, "raw_text": "anchor", "source_id": 10, "source_sort_order": 0},
        2: {"id": 2, "raw_text": "Ka-27 destroyed", "source_id": 11, "source_sort_order": 1},
        3: {"id": 3, "raw_text": "destruction of a Ka-27 confirmed", "source_id": 12, "source_sort_order": 2},
    }
    vec = {1: _band_vec(0), 2: _band_vec(28), 3: _band_vec(29)}  # 2~3 ≈ 1.0, both ~0.88 to 1

    confirmed, _ = await cd._confirm_mutes({2: 1, 3: 1}, item_by_id, {}, vec, {})

    assert calls["n"] == 2
    assert confirmed == {3: 2}             # muted under the other candidate, not the anchor


async def test_regroup_requires_the_pair_to_clear_the_cosine_floor(monkeypatch):
    """Embeddings stay the gate: the judge is never even asked about a pair whose own
    vectors never linked them."""
    judge, calls = _judge(lambda a, b: "same")
    monkeypatch.setattr(cd, "judge_pairs", judge)
    item_by_id = {
        2: {"id": 2, "raw_text": "a", "source_id": 11, "source_sort_order": 1},
        3: {"id": 3, "raw_text": "b", "source_id": 12, "source_sort_order": 2},
    }
    vec = {2: _band_vec(0), 3: _band_vec(60)}  # cosine 0.5, far below the floor

    assert await cd._regroup_rejected([(2, 1), (3, 1)], item_by_id, vec) == {}
    assert calls["n"] == 0


async def test_regroup_leaves_same_source_pairs_to_the_within_source_merge(monkeypatch):
    judge, _ = _judge(lambda a, b: "same")
    monkeypatch.setattr(cd, "judge_pairs", judge)
    item_by_id = {
        2: {"id": 2, "raw_text": "a", "source_id": 11, "source_sort_order": 1},
        3: {"id": 3, "raw_text": "b", "source_id": 11, "source_sort_order": 1},
    }
    vec = {2: _band_vec(0), 3: _band_vec(1)}

    assert await cd._regroup_rejected([(2, 1), (3, 1)], item_by_id, vec) == {}


async def test_regroup_never_crosses_categories(monkeypatch):
    """Muting across a category boundary would render the duplicate's link under a
    primary the reader meets in a different section."""
    judge, _ = _judge(lambda a, b: "same")
    monkeypatch.setattr(cd, "judge_pairs", judge)
    item_by_id = {
        2: {"id": 2, "raw_text": "a", "source_id": 11, "source_sort_order": 1, "category": "crypto"},
        3: {"id": 3, "raw_text": "b", "source_id": 12, "source_sort_order": 2, "category": "finance"},
    }
    vec = {2: _band_vec(0), 3: _band_vec(1)}  # cosine ~1.0, would otherwise collapse

    assert await cd._regroup_rejected([(2, 1), (3, 1)], item_by_id, vec) == {}

def test_sort_key_prefers_a_telegram_original_over_a_higher_ranked_feed():
    tg = {"id": 1, "source_type": "telegram", "source_sort_order": 9, "published_at": "2026-09-03"}
    rss = {"id": 2, "source_type": "rss", "source_sort_order": 0, "published_at": "2026-09-03"}

    assert min([tg, rss], key=cd._sort_key) is tg


def test_sort_key_falls_back_to_sort_order_when_the_preference_is_off(monkeypatch):
    monkeypatch.setattr(cd.settings, "primary_prefers_telegram", False)
    tg = {"id": 1, "source_type": "telegram", "source_sort_order": 9, "published_at": "2026-09-03"}
    rss = {"id": 2, "source_type": "rss", "source_sort_order": 0, "published_at": "2026-09-03"}

    assert min([tg, rss], key=cd._sort_key) is rss


def test_sort_key_keeps_sort_order_between_two_telegram_sources():
    early = {"id": 1, "source_type": "telegram", "source_sort_order": 0, "published_at": "2026-09-03T10:00"}
    late = {"id": 2, "source_type": "telegram", "source_sort_order": 3, "published_at": "2026-09-03T09:00"}

    assert min([early, late], key=cd._sort_key) is early


async def test_sent_pool_ignores_placeholder_vectors(monkeypatch):
    """A media-only post carries no readable text, so every one of them embeds to
    nearly the same point. Current items are already kept out of the vector map; a
    stored vector from an older backfill must not sneak into the sent pool either."""
    marked: list[tuple[int, int]] = []

    async def fake_recent(_hours):
        return [{"id": 99, "category": "feed", "source_id": 7, "published_at": "2026-09-01",
                 "sent": 1, "embedding": to_blob(_vec(1.0, 0.0)), "summary": "no text",
                 "source_sort_order": 0}]

    async def fake_mark(mid, pid):
        marked.append((mid, pid))

    async def fake_links(_ids):
        return {}

    monkeypatch.setattr(cd, "get_recent_embedded_items", fake_recent)
    monkeypatch.setattr(cd, "mark_duplicate", fake_mark)
    monkeypatch.setattr(cd, "get_duplicate_links", fake_links)
    monkeypatch.setattr(cd.settings, "dedup_shadow", False)
    # Without this the 2-d pool vector is rejected for its length and the test proves nothing.
    monkeypatch.setattr(cd, "from_blob", lambda b: np.frombuffer(b, dtype=np.float32) if b else None)

    items = [
        {"id": 1, "category": "feed", "source_id": 1, "source_sort_order": 0,
         "published_at": "2026-09-03", "summary": "Real news about a strike", "source_type": "telegram"},
        {"id": 2, "category": "feed", "source_id": 2, "source_sort_order": 1,
         "published_at": "2026-09-03", "summary": "Another unrelated story", "source_type": "telegram"},
    ]
    vec = {1: _vec(1.0, 0.0), 2: _vec(0.0, 1.0)}  # orthogonal: only the pool could link them

    survivors, _ = await cd.deduplicate(items, vec)

    assert [it["id"] for it in survivors] == [1, 2]
    assert marked == []


def _at(cos: float) -> np.ndarray:
    """A unit vector whose cosine with (1, 0) is exactly `cos`."""
    return _vec(cos, math.sqrt(1 - cos * cos))


def _row(i, src, cat="feed", **kw):
    return {"id": i, "category": cat, "source_id": src, "source_name": f"S{src}", "source_sort_order": src,
            "published_at": f"2026-10-0{i % 9 + 1}", "summary": f"story {i}", "raw_text": f"story {i}",
            "source_type": "telegram", **kw}


def _sent(i, src, vec, cat="feed"):
    return {"id": i, "category": cat, "source_id": src, "published_at": "2026-10-01", "sent": 1,
            "embedding": to_blob(vec), "summary": f"shown {i}", "source_sort_order": src}


def _wire(monkeypatch, sent=(), same_event=True):
    """Stub the DB and the LLM; returns the list of (muted, primary) marks."""
    marked: list[tuple[int, int]] = []

    async def fake_recent(_hours):
        return list(sent)

    async def fake_mark(mid, pid):
        marked.append((mid, pid))

    async def fake_links(_ids):
        return {}

    judge, _ = _judge(lambda a, b: "same" if same_event else "different")

    monkeypatch.setattr(cd, "get_recent_embedded_items", fake_recent)
    monkeypatch.setattr(cd, "mark_duplicate", fake_mark)
    monkeypatch.setattr(cd, "get_duplicate_links", fake_links)
    monkeypatch.setattr(cd, "judge_pairs", judge)
    # The real from_blob rejects any length but the live model's; these vectors are 2-d.
    monkeypatch.setattr(cd, "from_blob", lambda b: np.frombuffer(b, dtype=np.float32) if b else None)
    monkeypatch.setattr(cd.settings, "dedup_shadow", False)
    monkeypatch.setattr(cd.settings, "dedup_log_floor", 0.86)
    monkeypatch.setattr(cd.settings, "dedup_cross_category_threshold", 0.90)
    monkeypatch.setattr(cd.settings, "merge_near_dup_threshold", 0.975)
    monkeypatch.setattr(cd.settings, "dedup_auto_hide_threshold", 0.94)
    monkeypatch.setattr(cd.settings, "dedup_auto_hide_in_digest_threshold", 0.90)
    return marked


async def test_a_sources_next_post_on_a_shown_story_is_not_hidden(monkeypatch):
    """Index.hr's "court bans the strike" must not vanish under its own "strike day 3"
    from the previous digest: the same channel moving a story on is news."""
    marked = _wire(monkeypatch, sent=[_sent(99, 1, _vec(1, 0))])
    items = [_row(1, 1), _row(2, 2)]
    vec = {1: _at(0.90), 2: _vec(0, 1)}

    survivors, _ = await cd.deduplicate(items, vec)

    assert [it["id"] for it in survivors] == [1, 2]
    assert marked == []


async def test_a_sources_verbatim_repost_of_a_shown_post_is_hidden(monkeypatch):
    marked = _wire(monkeypatch, sent=[_sent(99, 1, _vec(1, 0))])
    items = [_row(1, 1), _row(2, 2)]
    vec = {1: _at(0.99), 2: _vec(0, 1)}

    survivors, _ = await cd.deduplicate(items, vec)

    assert [it["id"] for it in survivors] == [2]
    assert marked == [(1, 99)]


async def test_a_shown_story_mutes_only_what_matches_it_directly(monkeypatch):
    """Item 1 repeats a shown story; item 2 only resembles item 1. The old union-find
    chained item 2 onto the shown story too and hid it with no link."""
    marked = _wire(monkeypatch, sent=[_sent(99, 3, _vec(1, 0))])
    ang = math.acos(0.90)
    items = [_row(1, 1), _row(2, 2)]
    # 1~sent 0.90; 1~2 0.90; 2~sent = cos(2*ang) ~ 0.62
    vec = {1: _vec(math.cos(ang), math.sin(ang)), 2: _vec(math.cos(2 * ang), math.sin(2 * ang))}

    survivors, _ = await cd.deduplicate(items, vec)

    assert [it["id"] for it in survivors] == [2]
    assert marked == [(1, 99)]


async def test_a_cross_category_pair_needs_the_stricter_threshold(monkeypatch):
    marked = _wire(monkeypatch)
    items = [_row(1, 1, "feed"), _row(2, 2, "finance")]

    survivors, _ = await cd.deduplicate(items, {1: _vec(1, 0), 2: _at(0.88)})
    assert [it["id"] for it in survivors] == [1, 2]

    survivors, _ = await cd.deduplicate(items, {1: _vec(1, 0), 2: _at(0.93)})
    assert [it["id"] for it in survivors] == [1]
    assert marked == [(2, 1)]


async def test_a_shown_story_from_another_category_mutes_its_repeat(monkeypatch):
    """Бабель (feed) carried it in the morning; ПроБізнес (finance) repeats it at noon."""
    marked = _wire(monkeypatch, sent=[_sent(99, 3, _vec(1, 0), cat="feed")])
    items = [_row(1, 1, "finance"), _row(2, 2, "finance")]

    survivors, _ = await cd.deduplicate(items, {1: _at(0.93), 2: _vec(0, 1)})

    assert [it["id"] for it in survivors] == [2]
    assert marked == [(1, 99)]


def test_sort_key_prefers_the_post_that_says_more():
    """ПроБізнес's one-line «Польща та Румунія відмовили» vs Бабель's report with the
    reasons: the reasons win even from a source ranked lower."""
    short = {"id": 1, "source_type": "telegram", "source_sort_order": 0, "published_at": "2026-10-04T11:13",
             "raw_text": "Польща та Румунія відмовили Україні у збільшенні транзиту зерна, — Politico. → Про Бізнес"}
    long = {"id": 2, "source_type": "telegram", "source_sort_order": 5, "published_at": "2026-10-04T10:27",
            "raw_text": "Румунія та Польща заявили, що не готові розширювати транзит. " * 6}

    assert min([short, long], key=cd._sort_key) is long


def test_richness_ignores_links_and_context_prefixes():
    padded = {"raw_text": "[Context: " + "x" * 280 + "] short post https://example.com/" + "y" * 300}
    assert cd._richness(padded) == 0


async def test_an_item_rejected_against_the_sent_pool_still_collapses_with_its_partner(monkeypatch):
    """Item 1 scores 0.87 against an unrelated shown story, item 2 is the same story as
    item 1 from another source. B1 rejects 1→shown, and the pair must still collapse
    instead of shipping twice."""
    marked = _wire(monkeypatch, sent=[_sent(99, 3, _vec(1, 0))])

    judge, _ = _judge(lambda a, b: "different" if a == "shown 99" else "same")
    monkeypatch.setattr(cd, "judge_pairs", judge)
    a = math.acos(0.87)
    items = [_row(1, 1, raw_text="x" * 400), _row(2, 2)]
    # 1~2 at cos(0.35) ~ 0.94: below the near-identical pass, so only the B1 fallback can pair them.
    vec = {1: _vec(math.cos(a), math.sin(a)), 2: _vec(math.cos(a + 0.35), math.sin(a + 0.35))}

    survivors, _ = await cd.deduplicate(items, vec)

    assert [it["id"] for it in survivors] == [2]
    assert marked == [(1, 2)]


def test_sent_matches_skips_vectors_of_another_model():
    """A pool row embedded by a previous model has another length; it must be ignored,
    not crash the matrix product and with it the whole dedup pass."""
    item_by_id = {1: {"id": 1, "category": "feed", "source_id": 1}}
    meta = {9: (np.ones(3, dtype=np.float32), "feed", 2), 10: (_vec(1, 0), "feed", 2)}

    assert cd._sent_matches([(1, _vec(1, 0))], meta, item_by_id) == ([(1, 10, pytest.approx(1.0))], [])


async def test_no_fallback_through_a_primary_that_is_itself_hidden(monkeypatch):
    """Both items match shown stories; B1 confirms 1→99 and rejects 2→98. Item 2 must
    not then hide under item 1, which would land it under 99 — a story it was never
    compared with."""
    marked = _wire(monkeypatch, sent=[_sent(99, 3, _vec(1, 0)), _sent(98, 4, _vec(0, 1))])

    # 1 is the shown story 99; 1 and 2 would also read as one story if ever asked.
    judge, _ = _judge(lambda a, b: "different" if a == "shown 98" else "same")
    monkeypatch.setattr(cd, "judge_pairs", judge)
    # 1 is 0.92 to 99 and 2 is 0.92 to 98 on their own axes; 1~2 share only the third axis
    # (0.15), which the lowered floor lets union in this digest.
    items = [_row(1, 1), _row(2, 2)]
    v1 = np.array([0.92, 0.0, math.sqrt(1 - 0.92 ** 2)], dtype=np.float32)
    v2 = np.array([0.0, 0.92, math.sqrt(1 - 0.92 ** 2)], dtype=np.float32)
    monkeypatch.setattr(cd.settings, "dedup_log_floor", 0.15)
    monkeypatch.setattr(cd.settings, "dedup_cross_category_threshold", 0.90)
    sent = [_sent(99, 3, np.array([1, 0, 0], dtype=np.float32)), _sent(98, 4, np.array([0, 1, 0], dtype=np.float32))]

    async def fake_recent(_hours):
        return sent

    monkeypatch.setattr(cd, "get_recent_embedded_items", fake_recent)

    survivors, _ = await cd.deduplicate(items, {1: v1, 2: v2})

    assert [it["id"] for it in survivors] == [2]
    assert marked == [(1, 99)]


async def test_an_update_to_a_shown_story_stays_and_links_back(monkeypatch):
    """Another source moves a shown story on (a ruling after the strike): shown, with a
    ↻ link to the post the reader already saw."""
    marked = _wire(monkeypatch, sent=[_sent(99, 3, _vec(1, 0)) | {"original_url": "https://t.me/c/99",
                                                                   "published_at": "2026-10-06T08:00"}])
    judge, _ = _judge(lambda a, b: "update")
    monkeypatch.setattr(cd, "judge_pairs", judge)
    items = [_row(1, 1), _row(2, 2)]

    survivors, _ = await cd.deduplicate(items, {1: _at(0.90), 2: _vec(0, 1)})

    assert marked == []
    assert [it["id"] for it in survivors] == [1, 2]
    assert survivors[0]["_earlier"] == [("2026-10-06T08:00", "https://t.me/c/99")]
    assert "_earlier" not in survivors[1]


async def test_a_sources_follow_up_is_linked_back_only_when_the_judge_reads_one_story(monkeypatch):
    """Same-source pairs under the near-identical line are never hidden, and half of
    them are a different event in the same words: the ↻ link waits for the verdict."""
    shown = _sent(99, 1, _vec(1, 0)) | {"original_url": "https://t.me/c/99", "published_at": "2026-10-06T08:00"}
    for verdict, linked in (("update", True), ("same", True), ("different", False)):
        marked = _wire(monkeypatch, sent=[shown])
        judge, calls = _judge(lambda a, b, v=verdict: v)
        monkeypatch.setattr(cd, "judge_pairs", judge)
        items = [_row(1, 1), _row(2, 2)]

        survivors, _ = await cd.deduplicate(items, {1: _at(0.90), 2: _vec(0, 1)})

        assert marked == []
        assert [it["id"] for it in survivors] == [1, 2]
        assert ("_earlier" in survivors[0]) is linked
        assert calls["pairs"] == [("shown 99", "story 1")]


async def test_a_close_pair_is_hidden_without_asking_the_judge(monkeypatch):
    marked = _wire(monkeypatch)
    judge, calls = _judge(lambda a, b: "different")
    monkeypatch.setattr(cd, "judge_pairs", judge)
    items = [_row(1, 1), _row(2, 2)]

    survivors, _ = await cd.deduplicate(items, {1: _vec(1, 0), 2: _at(0.95)})

    assert [it["id"] for it in survivors] == [1]
    assert marked == [(2, 1)]
    assert calls["pairs"] == []


async def test_the_auto_hide_line_is_lower_inside_one_digest(monkeypatch):
    """At 0.92 a pair inside the digest hides without the judge (the hidden post stays
    linked beside its primary); the same cosine against a shown story still asks."""
    _wire(monkeypatch)
    judge, calls = _judge(lambda a, b: "different")
    monkeypatch.setattr(cd, "judge_pairs", judge)
    item_by_id = {1: {"id": 1, "raw_text": "p"}, 2: {"id": 2, "raw_text": "d"}, 3: {"id": 3, "raw_text": "e"}}
    vec = {1: _vec(1, 0), 2: _at(0.92), 3: _at(0.92)}

    confirmed, _ = await cd._confirm_mutes({2: 1, 3: 99}, item_by_id, {99: "shown"}, vec, {99: _vec(1, 0)})

    assert confirmed == {2: 1}
    assert calls["pairs"] == [("shown", "e")]


async def test_a_cross_category_pair_in_one_digest_still_goes_to_the_judge(monkeypatch):
    """The in-digest auto-hide line equals the cross-category candidate floor, so it
    must not apply across categories, or no such pair would ever be judged."""
    _wire(monkeypatch)
    judge, calls = _judge(lambda a, b: "different")
    monkeypatch.setattr(cd, "judge_pairs", judge)
    item_by_id = {1: {"id": 1, "raw_text": "p", "category": "feed"}, 2: {"id": 2, "raw_text": "d", "category": "finance"}}

    confirmed, _ = await cd._confirm_mutes({2: 1}, item_by_id, {}, {1: _vec(1, 0), 2: _at(0.92)}, {})

    assert confirmed == {}
    assert len(calls["pairs"]) == 1


async def test_an_update_never_hides_a_post_that_says_more_than_its_primary(monkeypatch):
    """A one-line Telegram primary must not bury the article carrying the new figure."""
    judge, _ = _judge(lambda a, b: "update")
    monkeypatch.setattr(cd, "judge_pairs", judge)
    item_by_id = {1: {"id": 1, "raw_text": "short"}, 2: {"id": 2, "raw_text": "detail " * 60},
                  3: {"id": 3, "raw_text": "short too"}}
    vec = {1: _vec(1, 0), 2: _at(0.88), 3: _at(0.88)}

    confirmed, updates = await cd._confirm_mutes({2: 1, 3: 1}, item_by_id, {}, vec, {})

    assert confirmed == {3: 1}
    assert updates == {}


async def test_an_update_hidden_in_the_digest_hands_its_link_to_the_post_that_stays(monkeypatch):
    """Item 1 continues a shown story; it is then hidden under item 2 from another
    source. Item 2 shows the story now, so it gets the ↻ link."""
    shown = _sent(99, 3, _vec(1, 0)) | {"original_url": "https://t.me/c/99", "published_at": "2026-10-06T08:00"}
    _wire(monkeypatch, sent=[shown])
    judge, _ = _judge(lambda a, b: "update" if a == "shown 99" else "same")
    monkeypatch.setattr(cd, "judge_pairs", judge)
    a = math.acos(0.88)
    items = [_row(1, 1), _row(2, 2, raw_text="x" * 400)]
    vec = {1: _vec(math.cos(a), math.sin(a)), 2: _vec(math.cos(a + 0.4), math.sin(a + 0.4))}

    survivors, _ = await cd.deduplicate(items, vec)

    assert [it["id"] for it in survivors] == [2]
    assert survivors[0]["_earlier"] == [("2026-10-06T08:00", "https://t.me/c/99")]


async def test_the_in_digest_auto_hide_never_buries_a_richer_post(monkeypatch):
    _wire(monkeypatch)
    judge, calls = _judge(lambda a, b: "different")
    monkeypatch.setattr(cd, "judge_pairs", judge)
    item_by_id = {1: {"id": 1, "raw_text": "one line"}, 2: {"id": 2, "raw_text": "detail " * 60}}

    confirmed, _ = await cd._confirm_mutes({2: 1}, item_by_id, {}, {1: _vec(1, 0), 2: _at(0.92)}, {})

    assert confirmed == {}
    assert len(calls["pairs"]) == 1


async def test_regroup_judges_only_pairs_linked_directly(monkeypatch):
    """A~B and B~C clear the floor, A~C does not: C is never put before the judge
    against A, whatever the judge would say."""
    judge, calls = _judge(lambda a, b: "same")
    monkeypatch.setattr(cd, "judge_pairs", judge)
    item_by_id = {i: {"id": i, "raw_text": f"t{i}", "source_id": 10 + i, "source_sort_order": i} for i in (1, 2, 3)}
    vec = {1: _band_vec(0), 2: _band_vec(29), 3: _band_vec(58)}  # 1~2, 2~3 ~0.875; 1~3 ~0.53

    out = await cd._regroup_rejected([(1, 9), (2, 9), (3, 9)], item_by_id, vec)

    assert out == {2: 1}
    assert [b for _a, b in calls["pairs"]] == ["t2"]


async def test_a_follow_up_is_still_linked_when_its_cross_source_match_is_rejected(monkeypatch):
    """The item resembles another source's shown post (judged different) and its own
    source's shown post: the ↻ link back to its own story must not be lost."""
    own = _sent(98, 1, _vec(1, 0)) | {"original_url": "https://t.me/c/98", "published_at": "2026-10-06T08:00"}
    # item 1 sits 0.90 from its own shown post and ~0.90 from the other source's one
    other = _sent(99, 3, _band_vec(math.degrees(2 * math.acos(0.90)))) | {"original_url": "https://t.me/c/99"}
    _wire(monkeypatch, sent=[own, other])
    judge, _ = _judge(lambda a, b: "update" if a == "shown 98" else "different")
    monkeypatch.setattr(cd, "judge_pairs", judge)
    items = [_row(1, 1), _row(2, 2)]

    survivors, _ = await cd.deduplicate(items, {1: _at(0.90), 2: _vec(0, -1)})

    assert [it["id"] for it in survivors] == [1, 2]
    assert survivors[0]["_earlier"] == [("2026-10-06T08:00", "https://t.me/c/98")]


async def test_even_a_close_pair_never_buries_a_richer_post_without_a_verdict(monkeypatch):
    _wire(monkeypatch)
    judge, calls = _judge(lambda a, b: "different")
    monkeypatch.setattr(cd, "judge_pairs", judge)
    item_by_id = {1: {"id": 1, "raw_text": "one line"}, 2: {"id": 2, "raw_text": "detail " * 60}}

    confirmed, _ = await cd._confirm_mutes({2: 1}, item_by_id, {}, {1: _vec(1, 0), 2: _at(0.96)}, {})

    assert confirmed == {}
    assert len(calls["pairs"]) == 1


async def test_a_close_pair_never_buries_a_richer_post_under_a_shown_one_without_a_verdict(monkeypatch):
    """The 09:45 one-liner was shown; the 14:45 article with the new toll scores 0.95
    against it and must reach the judge, whose "update" keeps it with a link back."""
    _wire(monkeypatch)
    judge, calls = _judge(lambda a, b: "update")
    monkeypatch.setattr(cd, "judge_pairs", judge)
    item_by_id = {2: {"id": 2, "raw_text": "detail " * 60}}

    confirmed, updates = await cd._confirm_mutes({2: 99}, item_by_id, {99: "one line"}, {2: _at(0.95)}, {99: _vec(1, 0)})

    assert confirmed == {}
    assert updates == {2: 99}

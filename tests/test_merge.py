"""Within-source merge robustness: a misbehaving group_by_topic (empty or
out-of-range `ids`) must never crash the digest. Regression for the
`max() iterable argument is empty` ValueError that killed two prod digests
on 2026-06-29 when the LLM returned a group with empty ids."""
import numpy as np

import src.processor.dedup.merge as mg
from src.dispatcher import digest_builder
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

def _timed(i, pub, url):
    return {"id": i, "summary": "s", "raw_text": "s", "key_phrase": "",
            "published_at": pub, "original_url": url}


def test_merged_story_links_the_newest_post_and_keeps_the_earlier_ones():
    cluster = [_timed(2, "2026-09-23T23:45:00+00:00", "https://t.me/l/2"),
               _timed(1, "2026-09-23T23:07:00+00:00", "https://t.me/l/1"),
               _timed(3, "2026-09-24T04:45:00+00:00", "https://t.me/l/3")]
    merged = mg._build_merged(cluster, "Київ: загинули двоє", "загинули двоє")
    assert merged["original_url"] == "https://t.me/l/3"
    assert merged["summary"] == "Київ: загинули двоє"
    assert merged["_earlier"] == [("2026-09-23T23:07:00+00:00", "https://t.me/l/1"),
                                  ("2026-09-23T23:45:00+00:00", "https://t.me/l/2")]
    assert sorted(merged["_item_ids"]) == [1, 2, 3]


def test_merged_story_renders_its_earlier_updates_as_time_links(monkeypatch):
    monkeypatch.setattr(digest_builder.settings, "digest_timezone", "UTC")
    merged = mg._build_merged(
        [_timed(1, "2026-09-23T23:07:00+00:00", "https://t.me/l/1"),
         _timed(3, "2026-09-24T04:45:00+00:00", "https://t.me/l/3")],
        "Київ: загинули двоє", "загинули двоє")
    line = digest_builder._format_item(merged)
    first, second = line.split("\n")
    assert 'href="https://t.me/l/3"' in first
    assert second == '<i>↻ earlier: <a href="https://t.me/l/1">23:07</a></i>'


def test_an_ordinary_item_has_no_earlier_line():
    assert "\n" not in digest_builder._format_item(_timed(1, None, "https://t.me/l/1"))


def test_fallback_summary_is_the_newest_posts_not_the_longest():
    # The merged line links the newest post, so its text must not be an older,
    # longer post's superseded figure.
    cluster = [dict(_timed(1, "2026-09-23T23:07:00+00:00", "https://t.me/l/1"), summary="Загинув один, двоє поранені, пошкоджено будинок"),
               dict(_timed(2, "2026-09-24T04:45:00+00:00", "https://t.me/l/2"), summary="Загинули двоє")]
    assert mg._cluster_summary_fields(cluster)[0] == "Загинули двоє"


def test_merged_line_is_stamped_with_the_post_it_links():
    cluster = [_timed(1, "2026-09-23T23:07:00+00:00", "https://t.me/l/1"),
               _timed(2, "2026-09-24T06:00:00+00:00", None)]
    merged = mg._build_merged(cluster, "s", "")
    assert merged["original_url"] == "https://t.me/l/1"
    assert merged["published_at"] == "2026-09-23T23:07:00+00:00"


def test_a_story_reposted_all_night_links_only_its_latest_earlier_posts(monkeypatch):
    monkeypatch.setattr(digest_builder.settings, "digest_timezone", "UTC")
    cluster = [_timed(i, f"2026-09-24T0{i}:00:00+00:00", f"https://t.me/l/{i}") for i in range(8)]
    line = digest_builder._earlier_line(mg._build_merged(cluster, "s", ""))
    assert line.startswith("<i>↻ earlier: +3 · ")
    assert line.count("<a ") == digest_builder._EARLIER_MAX_LINKS
    assert ">06:00</a>" in line and ">02:00</a>" not in line


async def test_group_without_a_summary_takes_the_newest_posts_text(monkeypatch):
    items = [dict(_timed(1, "2026-09-23T23:07:00+00:00", "https://t.me/l/1"), summary="Загинув один"),
             dict(_timed(2, "2026-09-24T04:45:00+00:00", "https://t.me/l/2"), summary="Загинули двоє"),
             dict(_timed(3, "2026-09-24T05:00:00+00:00", "https://t.me/l/3"), summary="Інше"),
             dict(_timed(4, "2026-09-24T05:10:00+00:00", "https://t.me/l/4"), summary="Ще інше")]

    async def fake_group_by_topic(inputs, prompt_extra=None):
        return [{"ids": [0, 1], "summary": "", "key_phrase": ""},
                {"ids": [2], "summary": "Інше", "key_phrase": ""},
                {"ids": [3], "summary": "Ще інше", "key_phrase": ""}]

    monkeypatch.setattr(mg, "group_by_topic", fake_group_by_topic)
    monkeypatch.setattr(mg, "is_task_dead", lambda task: False)
    out = await mg._merge_via_group_by_topic(items)
    assert out[0]["summary"] == "Загинули двоє"
    assert out[0]["original_url"] == "https://t.me/l/2"


def test_a_merged_line_keeps_a_members_link_to_a_story_shown_earlier():
    """Dedup can tag an item as an update to a story shown in an earlier digest; folding
    it into a same-source cluster must not drop that ↻ link."""
    from src.processor.dedup.merge import _build_merged
    a = {"id": 1, "summary": "s1", "published_at": "2026-10-07T09:00", "original_url": "u1",
         "_earlier": [("2026-10-06T08:00", "u0")]}
    b = {"id": 2, "summary": "s2", "published_at": "2026-10-07T10:00", "original_url": "u2"}

    merged = _build_merged([b, a], "s", "")

    assert merged["original_url"] == "u2"
    assert merged["_earlier"] == [("2026-10-06T08:00", "u0"), ("2026-10-07T09:00", "u1")]


def test_an_unmerged_item_keeps_its_link_to_a_story_shown_earlier():
    """Most updates stay single in their source's block; the plain copy the merge makes
    of them must carry the ↻ link too."""
    item = {"id": 5, "summary": "s", "key_phrase": "", "original_url": "u5", "published_at": "2026-10-07T09:00",
            "raw_text": "r", "_earlier": [("2026-10-06T08:00", "u0")]}

    assert mg._items_as_plain([item])[0]["_earlier"] == [("2026-10-06T08:00", "u0")]


def test_a_link_back_to_another_days_post_carries_its_date(monkeypatch):
    monkeypatch.setattr(digest_builder.settings, "digest_timezone", "UTC")
    item = {"summary": "s", "original_url": "u", "published_at": "2026-10-07T12:00:00+00:00",
            "_earlier": [("2026-10-06T21:30:00+00:00", "https://t.me/l/0"), ("2026-10-07T04:00:00+00:00", "https://t.me/l/1")]}

    assert digest_builder._earlier_line(item) == (
        '<i>↻ earlier: <a href="https://t.me/l/0">06/10 21:30</a>, <a href="https://t.me/l/1">04:00</a></i>')


def test_a_long_line_never_cuts_its_link_to_another_digest(monkeypatch):
    """Six overnight reposts plus a link back to yesterday's shown story: the reposts
    give way first."""
    monkeypatch.setattr(digest_builder.settings, "digest_timezone", "UTC")
    item = {"summary": "s", "original_url": "u", "published_at": "2026-10-07T09:00:00+00:00",
            "_earlier": [("2026-10-06T08:00:00+00:00", "https://t.me/l/old")]
                        + [(f"2026-10-07T0{h}:00:00+00:00", f"https://t.me/l/{h}") for h in range(1, 7)]}

    line = digest_builder._earlier_line(item)

    assert line.startswith("<i>↻ earlier: +3 · ")
    assert ">06/10 08:00</a>" in line and ">06:00</a>" in line and ">03:00</a>" not in line

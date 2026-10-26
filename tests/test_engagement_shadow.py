"""Shadow engagement: the digest logs the queued Telegram posts with the most views
against their channel's own median, and changes nothing else."""
import logging

import pytest

import src.dispatcher.digest_builder as db


def _item(item_id, source_id, views):
    return {"id": item_id, "source_id": source_id, "source_name": f"S{source_id}", "views": views,
            "forwards": 1, "reactions": 2, "published_at": "2026-10-26T06:00:00+00:00",
            "engagement_at": "2026-10-26 09:30:00",
            "raw_text": f"Post {item_id}\nmore"}


@pytest.mark.asyncio
async def test_posts_are_ranked_against_their_own_channel(monkeypatch, caplog):
    async def baseline(source_ids):
        assert source_ids == [1, 2]
        return {1: [100] * 10, 2: [10_000] * 10, 3: [5] * 3}
    monkeypatch.setattr(db, "get_views_baseline", baseline)
    items = [_item(1, 1, 400), _item(2, 2, 12_000), _item(3, 1, None), _item(4, 2, 5_000)]
    with caplog.at_level(logging.INFO, logger=db.log.name):
        await db._log_engagement(items)

    lines = [r.message for r in caplog.records if "Engagement (shadow)" in r.message]
    assert [l.split()[3] for l in lines] == ["id=1", "id=2", "id=4"]  # ×4.0, ×1.2, ×0.5
    assert "×4.0 of median" in lines[0] and "age 3.5h" in lines[0] and "| Post 1" in lines[0]


@pytest.mark.asyncio
async def test_a_channel_with_too_few_measured_posts_is_left_out(monkeypatch, caplog):
    async def baseline(source_ids):
        return {1: [100] * (db._ENGAGEMENT_MIN_BASELINE - 1)}
    monkeypatch.setattr(db, "get_views_baseline", baseline)
    with caplog.at_level(logging.INFO, logger=db.log.name):
        await db._log_engagement([_item(1, 1, 400)])
    assert "Engagement (shadow)" not in caplog.text


@pytest.mark.asyncio
async def test_a_failure_costs_only_the_log_line(monkeypatch):
    async def boom(source_ids):
        raise RuntimeError("locked")
    monkeypatch.setattr(db, "get_views_baseline", boom)
    await db._log_engagement([_item(1, 1, 400)])


@pytest.mark.asyncio
async def test_an_orphan_item_does_not_drop_the_whole_log(monkeypatch, caplog):
    async def baseline(source_ids):
        assert source_ids == [1]
        return {1: [100] * 10}
    monkeypatch.setattr(db, "get_views_baseline", baseline)
    with caplog.at_level(logging.INFO, logger=db.log.name):
        await db._log_engagement([_item(1, 1, 400), _item(2, None, 900)])
    assert "Engagement (shadow): item id=1" in caplog.text


def test_an_unknown_measure_time_reads_as_a_question_mark():
    assert db._measured_age({"published_at": "2026-10-26T06:00:00+00:00", "engagement_at": None}) == "?"

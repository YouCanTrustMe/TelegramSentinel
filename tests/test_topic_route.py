"""Topic routing for mixed-topic sources: of 114 Hacker News stories at 300+ points in
the week to 2026-10-26, ~20% were politics, economy or science, all shown under tech.
The model only picks a category from a fixed list; shadow mode only logs the pick."""
import logging

import pytest

import src.db.models as models
import src.processor.llm.classifier as cl
from src.config import settings


def _fake_llm(answer):
    calls = []

    async def fake(messages, max_retries=3, task="classify"):
        calls.append((task, messages))
        return answer
    return fake, calls


@pytest.mark.asyncio
async def test_only_known_ids_and_listed_categories_are_picked(monkeypatch):
    fake, calls = _fake_llm({"route": [
        {"id": 1, "section": "Finance"},
        {"id": 2, "section": "sports"},       # not a target
        {"id": 99, "section": "feed"},        # not in this chunk
        {"id": True, "section": "feed"},      # a bool is not an id
        {"id": "3", "section": "feed"},
        "garbage",
    ]})
    monkeypatch.setattr(cl, "llm_json", fake)
    picks = await cl.route_by_topic([{"id": i, "text": f"story {i}"} for i in (1, 2, 3)])

    assert picks == {1: "finance", 3: "feed"}
    assert calls[0][0] == "route"
    assert '"finance":' in calls[0][1][0]["content"]


@pytest.mark.asyncio
async def test_a_failed_chunk_moves_nothing_and_the_next_chunk_still_runs(monkeypatch, caplog):
    calls = []

    async def flaky(messages, max_retries=3, task="classify"):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("down")
        return {"route": [{"id": cl._ROUTE_CHUNK + 1, "section": "feed"}]}
    monkeypatch.setattr(cl, "llm_json", flaky)
    items = [{"id": i, "text": "x"} for i in range(1, cl._ROUTE_CHUNK + 2)]
    with caplog.at_level(logging.INFO, logger=cl.log.name):
        assert await cl.route_by_topic(items) == {cl._ROUTE_CHUNK + 1: "feed"}
    # A shadow feature never pages the admin.
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.asyncio
async def test_a_failing_write_never_escapes_the_pass(monkeypatch):
    async def fake_route(items):
        return {1: "finance"}

    async def locked(*a):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(cl, "route_by_topic", fake_route)
    monkeypatch.setattr(models, "category_exists", locked)
    monkeypatch.setattr(settings, "route_by_topic_shadow", False)
    await cl._route_classified([_row(1)], {1})


@pytest.mark.asyncio
async def test_a_shadow_pick_runs_outside_the_lock_and_a_live_move_inside_it(monkeypatch):
    seen = []

    async def fake_pass(limit):
        return [_row(1)], {1}

    async def fake_route_classified(rows, done):
        seen.append(cl.background_classify_running())
    monkeypatch.setattr(cl, "_classify_pending_items", fake_pass)
    monkeypatch.setattr(cl, "_route_classified", fake_route_classified)
    monkeypatch.setattr(settings, "route_by_topic_shadow", True)
    await cl.classify_pending_items(limit=3)
    # A live move lands before a digest waiting on the lock reads the queue.
    monkeypatch.setattr(settings, "route_by_topic_shadow", False)
    await cl.classify_pending_items(limit=3)
    assert seen == [False, True]


def _row(item_id, url="https://hnrss.org/newest?points=300", category="tech"):
    return {"id": item_id, "source_url": url, "category": category, "raw_text": f"Story {item_id}\nbody"}


@pytest.mark.asyncio
async def test_shadow_logs_the_pick_and_leaves_the_item(monkeypatch, caplog):
    async def fake_route(items):
        assert [i["id"] for i in items] == [1, 2]  # 3 is not from a listed host, 4 was not summarised
        return {1: "finance"}
    writes = []

    async def fake_set(item_id, category):
        writes.append((item_id, category))
    monkeypatch.setattr(cl, "route_by_topic", fake_route)
    monkeypatch.setattr(models, "set_item_category", fake_set)
    monkeypatch.setattr(settings, "route_by_topic_shadow", True)
    rows = [_row(1), _row(2), _row(3, url="https://www.techmeme.com/feed.xml"), _row(4)]
    with caplog.at_level(logging.INFO, logger=cl.log.name):
        await cl._route_classified(rows, {1, 2, 3})

    assert writes == []
    text = caplog.text
    assert "Topic route (shadow): item id=1 tech → finance | Story 1" in text
    assert "Topic route: item id=2 stays in tech" in text


@pytest.mark.asyncio
async def test_live_mode_moves_only_into_an_existing_category(monkeypatch):
    async def fake_route(items):
        return {1: "finance", 2: "hrvatska"}
    writes = []

    async def fake_set(item_id, category):
        writes.append((item_id, category))

    async def fake_exists(name):
        return name == "finance"
    monkeypatch.setattr(cl, "route_by_topic", fake_route)
    monkeypatch.setattr(models, "set_item_category", fake_set)
    monkeypatch.setattr(models, "category_exists", fake_exists)
    monkeypatch.setattr(settings, "route_by_topic_shadow", False)
    await cl._route_classified([_row(1), _row(2)], {1, 2})

    assert writes == [(1, "finance")]

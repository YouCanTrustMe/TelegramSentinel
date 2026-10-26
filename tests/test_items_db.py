"""Item queries against a real SQLite file."""
from datetime import datetime, timezone

import pytest

from src.config import settings
from src.db.base import init_db
from src.db.items import get_recent_embedded_items, mark_blocked, mark_sent, save_item, set_item_embeddings
from src.db.sources import add_source


@pytest.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", str(tmp_path / "sentinel.db"))
    await init_db()


async def test_a_blocked_item_is_not_in_the_dedup_sent_pool(db):
    """A blocked post is marked sent but was never shown, so a real report must not be
    muted as its repeat (2026-10: 21 mutes in 20 days, ~7 of them real news)."""
    sid = await add_source("telegram", "A", "@a", "feed")
    now = datetime.now(timezone.utc).isoformat()
    shown = await save_item(sid, "tg_a_1", "shown post", "https://t.me/a/1", now, "shown", "feed", now)
    blocked = await save_item(sid, "tg_a_2", "alert", None, now, "alert", "feed", now)
    await set_item_embeddings([(shown, b"\0" * 8), (blocked, b"\0" * 8)])
    await mark_sent([shown])
    await mark_blocked([(blocked, "air raid alerts")])

    rows = await get_recent_embedded_items(48)
    assert [r["id"] for r in rows] == [shown]
    # B1 judges the raw post, and an update links back to it.
    assert (rows[0]["raw_text"], rows[0]["original_url"]) == ("shown post", "https://t.me/a/1")


async def test_the_backfill_rows_carry_their_sources_prompt_extra(db):
    """classify reads "no translate" from source_prompt_extra; the backfill query used
    SELECT * on items alone, so a no-translate source's post got translated there."""
    from src.db.base import get_db
    from src.db.items import get_sent_empty_items

    sid = await add_source("rss", "A", "https://a/feed", "dev")
    async with get_db() as conn:
        await conn.execute("UPDATE sources SET prompt_extra = 'no translate' WHERE id = ?", (sid,))
        await conn.commit()
    now = datetime.now(timezone.utc).isoformat()
    iid = await save_item(sid, "a1", "Short English post", None, now, "", "dev", now)
    await mark_sent([iid])

    (row,) = await get_sent_empty_items(5)
    assert row["source_prompt_extra"] == "no translate"


async def test_engagement_refreshes_only_queued_items(db):
    """A sent post keeps the views it had when it went out; the shadow comparison is
    of what the reader would have seen at digest time."""
    from src.db.base import get_db
    from src.db.items import get_views_baseline, set_item_engagement

    sid = await add_source("telegram", "A", "@a", "feed")
    now = datetime.now(timezone.utc).isoformat()
    queued = await save_item(sid, "tg_a_1", "queued", None, now, "", "feed", now)
    sent = await save_item(sid, "tg_a_2", "sent", None, now, "", "feed", now)
    await mark_sent([sent])
    await set_item_engagement(sid, [(900, 12, 30, "tg_a_1"), (5000, 1, 2, "tg_a_2"), (1, 1, 1, "tg_a_missing")])

    async with get_db() as conn:
        async with conn.execute("SELECT id, views, forwards, reactions, engagement_at FROM items ORDER BY id") as cur:
            rows = {r["id"]: r for r in await cur.fetchall()}
    assert (rows[queued]["views"], rows[queued]["forwards"], rows[queued]["reactions"]) == (900, 12, 30)
    assert rows[queued]["engagement_at"] is not None
    assert rows[sent]["views"] is None
    # The queued post is what gets scored, so it is not part of its own baseline.
    assert await get_views_baseline([sid]) == {}
    await mark_sent([queued])
    assert await get_views_baseline([sid]) == {sid: [900]}
    # A muted duplicate or a filtered post never went out.
    muted = await save_item(sid, "tg_a_3", "dup", None, now, "", "feed", now)
    await set_item_engagement(sid, [(3, 0, 0, "tg_a_3")])
    await mark_blocked([(muted, "ads")])
    assert await get_views_baseline([sid]) == {sid: [900]}


async def test_the_queue_rows_carry_their_sources_url(db):
    """Topic routing picks Hacker News stories by the feed's host."""
    from src.db.items import get_unsent_items

    sid = await add_source("rss", "HN", "https://hnrss.org/newest?points=300", "tech")
    now = datetime.now(timezone.utc).isoformat()
    await save_item(sid, "hn1", "Story", None, now, "", "tech", now)
    (row,) = await get_unsent_items()
    assert row["source_url"] == "https://hnrss.org/newest?points=300"

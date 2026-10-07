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

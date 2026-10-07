"""Source-health alerts against a real SQLite file: the RSS re-probe that tells a
feed's own short outage (heals in the hour, says nothing) from one still down hours later."""
import pytest

from src.config import settings
from src.db.base import get_db, init_db
from src.db.sources import add_source, revive_error_rss_sources


@pytest.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "database_path", str(tmp_path / "sentinel.db"))
    await init_db()


async def _rss(name: str, fails: int) -> int:
    sid = await add_source("rss", name, f"https://{name}.example/feed", "ai")
    async with get_db() as conn:
        await conn.execute("UPDATE sources SET status='error', fail_count=? WHERE id=?", (fails, sid))
        await conn.commit()
    return sid


async def test_the_frequent_reprobe_only_revives_feeds_down_for_a_few_hours(db):
    await _rss("fresh", 4)
    await _rss("stale", 30)

    assert await revive_error_rss_sources(12) == ["fresh"]


async def test_the_daily_reprobe_revives_every_failed_feed(db):
    await _rss("fresh", 4)
    await _rss("stale", 30)

    assert sorted(await revive_error_rss_sources()) == ["fresh", "stale"]

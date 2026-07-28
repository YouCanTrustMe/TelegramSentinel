"""The normalised digest schedule against a real SQLite file: the migration that
splits the old comma string, and the slot-level writes the timetable performs.

Pure-function tests cannot cover this — the migration is SQL, and it is the one
piece that runs exactly once against live data.
"""
import asyncio

import pytest

from src.config import settings
from src.db.base import get_db, init_db


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "sentinel.db"
    monkeypatch.setattr(settings, "database_path", str(path))
    return path


def _seed_legacy(path):
    """Bring a DB up to the migration just before this one and fill it with the
    digest_time shapes production actually accumulated, so the split runs against
    the same input the live database will hand it."""
    import sqlite3
    from pathlib import Path

    import src.db.base as base

    migrations = sorted((Path(base.__file__).parent / "migrations").glob("*.sql"))
    db = sqlite3.connect(path)
    db.executescript("CREATE TABLE IF NOT EXISTS _migrations (name TEXT PRIMARY KEY)")
    for migration in migrations:
        if migration.name >= "027":
            break
        db.executescript(migration.read_text())
        db.execute("INSERT INTO _migrations (name) VALUES (?)", (migration.name,))
    db.executemany(
        "INSERT INTO categories (name, emoji, digest_time, sort_order) VALUES (?,?,?,?)",
        [
            ("feed", "📰", "11:00, 16:00, 21:00", 0),
            ("ai", "💠", "11:00,21:00", 1),
            ("legacy", "🧪", "9:30", 2),
            ("dupes", "🧪", "11:00,11:00", 3),
            ("bad", "🧪", "25:99,12:00", 4),
            ("empty", "🧪", "", 5),
        ],
    )
    db.commit()
    db.close()


def test_migration_splits_the_legacy_string(db_path):
    _seed_legacy(db_path)

    async def run():
        await init_db()
        from src.db.models import get_categories
        return {c["name"]: c["digest_time"] for c in await get_categories()}

    times = asyncio.run(run())
    assert times["feed"] == "11:00,16:00,21:00"
    assert times["ai"] == "11:00,21:00"
    assert times["legacy"] == "09:30"        # zero-padded on the way in
    assert times["dupes"] == "11:00"         # the duplicate collapses
    assert times["bad"] == "12:00"           # 25:99 could never fire, so it is dropped
    assert times["empty"] == ""              # shows up as "never" in the timetable


def test_schema_rejects_what_the_string_used_to_allow(db_path):
    _seed_legacy(db_path)

    async def run():
        await init_db()
        import aiosqlite
        errors = []
        for bad in [("feed", 25, 0), ("feed", 12, 99)]:
            try:
                async with get_db() as db:
                    await db.execute("INSERT INTO category_times VALUES (?,?,?)", bad)
                    await db.commit()
            except aiosqlite.IntegrityError as exc:
                errors.append(str(exc))
        # a duplicate is impossible now, not merely tidied up afterwards
        async with get_db() as db:
            await db.execute("INSERT OR IGNORE INTO category_times VALUES ('feed',11,0)")
            await db.commit()
            async with db.execute(
                "SELECT COUNT(*) c FROM category_times WHERE category='feed' AND hour=11"
            ) as cur:
                dupes = (await cur.fetchone())["c"]
        return errors, dupes

    errors, dupes = asyncio.run(run())
    assert len(errors) == 2 and all("CHECK constraint" in e for e in errors)
    assert dupes == 1


def test_slot_writes_and_schedule_readback(db_path):
    _seed_legacy(db_path)

    async def run():
        await init_db()
        from src.db.models import (
            add_category_time,
            get_categories,
            get_schedule_slots,
            remove_category_time,
            remove_time_everywhere,
            update_category,
        )
        await add_category_time("ai", 16, 0)
        await remove_category_time("ai", 11, 0)
        after_toggle = {c["name"]: c["digest_time"] for c in await get_categories()}

        slots = await get_schedule_slots()

        # a rename must carry the schedule with it, or the category silently stops
        await update_category("ai", new_name="ml")
        renamed = {c["name"]: c["digest_time"] for c in await get_categories()}

        removed = await remove_time_everywhere(16, 0)
        after_remove = await get_schedule_slots()
        return after_toggle, slots, renamed, removed, after_remove

    after_toggle, slots, renamed, removed, after_remove = asyncio.run(run())
    assert after_toggle["ai"] == "16:00,21:00"
    assert slots["16:00"] == ["feed", "ai"]  # ordered by the categories' sort_order
    assert list(slots) == ["09:30", "11:00", "12:00", "16:00", "21:00"]
    assert "ai" not in renamed and renamed["ml"] == "16:00,21:00"
    assert removed == 2 and "16:00" not in after_remove


def test_removing_a_category_takes_its_times(db_path):
    _seed_legacy(db_path)

    async def run():
        await init_db()
        from src.db.models import get_schedule_slots, remove_category

        await remove_category("feed")
        async with get_db() as db:
            async with db.execute(
                "SELECT COUNT(*) c FROM category_times WHERE category='feed'"
            ) as cur:
                left = (await cur.fetchone())["c"]
        return left, await get_schedule_slots()

    left, slots = asyncio.run(run())
    assert left == 0
    assert "16:00" not in slots  # feed was the only category running then

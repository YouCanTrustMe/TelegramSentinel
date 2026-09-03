"""Connection pragmas and the write-lock retry: a dozen parallel RSS writers plus the
digest used to surface SQLITE_BUSY as an ERROR that dropped the rest of a feed's poll."""
import sqlite3

import pytest

import src.db.base as base


async def test_get_db_applies_the_concurrency_pragmas(tmp_path, monkeypatch):
    monkeypatch.setattr(base.settings, "database_path", str(tmp_path / "t.db"))

    async with base.get_db() as db:
        async with db.execute("PRAGMA busy_timeout") as cur:
            assert (await cur.fetchone())[0] == base._BUSY_TIMEOUT_MS
        async with db.execute("PRAGMA synchronous") as cur:
            assert (await cur.fetchone())[0] == 1  # NORMAL


async def test_retry_on_locked_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr(base, "_LOCK_RETRY_DELAY", 0)
    calls = {"n": 0}

    @base.retry_on_locked
    async def write():
        calls["n"] += 1
        if calls["n"] < 3:
            raise sqlite3.OperationalError("database is locked")
        return "written"

    assert await write() == "written"
    assert calls["n"] == 3


async def test_retry_on_locked_gives_up_after_the_last_attempt(monkeypatch):
    monkeypatch.setattr(base, "_LOCK_RETRY_DELAY", 0)
    calls = {"n": 0}

    @base.retry_on_locked
    async def write():
        calls["n"] += 1
        raise sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError):
        await write()
    assert calls["n"] == base._LOCK_RETRIES


async def test_retry_on_locked_reraises_other_errors_immediately(monkeypatch):
    monkeypatch.setattr(base, "_LOCK_RETRY_DELAY", 0)
    calls = {"n": 0}

    @base.retry_on_locked
    async def write():
        calls["n"] += 1
        raise sqlite3.OperationalError("no such column: nope")

    with pytest.raises(sqlite3.OperationalError):
        await write()
    assert calls["n"] == 1


def test_every_collector_hot_path_write_is_retried():
    """The lock surfaced inside an RSS poll, and a poll writes through both modules:
    save_item for each entry and the source bookkeeping around it (fail counters,
    status, the telegram bookmark). An unwrapped one still aborts the whole feed."""
    import src.db.items as items
    import src.db.sources as sources

    wrapped = {"__wrapped__"}
    for module, names in (
        (items, ["save_item", "set_item_embeddings", "mark_duplicate", "mark_sent",
                 "mark_blocked", "update_item_classification"]),
        (sources, ["set_source_last_message_id", "increment_source_fail_count",
                   "reset_source_fail_count", "update_source_status", "set_source_chat_id"]),
    ):
        for name in names:
            assert wrapped & set(vars(getattr(module, name))), f"{name} is not retried"

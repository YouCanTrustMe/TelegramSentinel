"""Database foundation: the shared connection context manager, migration
runner and generic app-settings access. Domain modules (sources, items,
categories, blocked) build on top of get_db()."""
import asyncio
import logging
import sqlite3
import aiosqlite
from contextlib import asynccontextmanager
from functools import wraps
from pathlib import Path

from src.config import settings

log = logging.getLogger(__name__)


# A dozen RSS feeds are polled in parallel, each writing through its own connection,
# while the collector and the digest write through theirs. Under a disk stall on the
# 1 GB box that queue outgrew the old 5s wait and surfaced as "database is locked".
_BUSY_TIMEOUT_MS = 30000
_LOCK_RETRIES = 3
_LOCK_RETRY_DELAY = 0.4


@asynccontextmanager
async def get_db():
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")
        # collectors + scheduler write concurrently through their own connections
        await db.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        # WAL already survives a crash at NORMAL; it only loses the last commits on a
        # host power cut, and that trade buys an fsync-free commit on a slow volume.
        await db.execute("PRAGMA synchronous = NORMAL")
        yield db


def retry_on_locked(func):
    """Retry a write that lost the race for the write lock. busy_timeout covers the
    common contention, but a writer that is itself mid-transaction gets SQLITE_BUSY
    returned immediately, without the busy handler ever running — one retry then costs
    a fraction of a second and saves the caller's whole pass (an RSS feed used to drop
    the rest of its entries for a full poll interval)."""
    @wraps(func)
    async def wrapper(*args, **kwargs):
        for attempt in range(1, _LOCK_RETRIES + 1):
            try:
                return await func(*args, **kwargs)
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == _LOCK_RETRIES:
                    raise
                log.warning("DB write %s locked, retry %d/%d in %.1fs",
                            func.__name__, attempt, _LOCK_RETRIES - 1, _LOCK_RETRY_DELAY)
                await asyncio.sleep(_LOCK_RETRY_DELAY * attempt)
    return wrapper


def _split_sql_statements(sql: str) -> list[str]:
    out: list[str] = []
    cur: list[str] = []
    quote: str | None = None
    for ch in sql:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
        elif ch in ("'", '"'):
            quote = ch
            cur.append(ch)
        elif ch == ";":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    if cur:
        out.append("".join(cur))
    return out


async def _schema_has_migration(db, migration) -> bool:
    import re
    sql = migration.read_text()
    for stmt in _split_sql_statements(sql):
        stmt_up = stmt.strip().upper()
        if not stmt_up:
            continue
        m = re.search(r"CREATE TABLE\s+IF NOT EXISTS\s+(\w+)", stmt_up)
        if m:
            async with db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (m.group(1).lower(),),
            ) as cur:
                if not await cur.fetchone():
                    return False
            continue
        m = re.search(r"ALTER TABLE\s+(\w+)\s+ADD COLUMN\s+(\w+)", stmt_up)
        if m:
            table, col = m.group(1).lower(), m.group(2).lower()
            async with db.execute(
                f"SELECT COUNT(*) FROM pragma_table_info('{table}') WHERE name=?", (col,)
            ) as cur:
                if (await cur.fetchone())[0] == 0:
                    return False
            continue
        m = re.search(r"ALTER TABLE\s+(\w+)\s+DROP COLUMN\s+(\w+)", stmt_up)
        if m:
            table, col = m.group(1).lower(), m.group(2).lower()
            async with db.execute(
                f"SELECT COUNT(*) FROM pragma_table_info('{table}') WHERE name=?", (col,)
            ) as cur:
                if (await cur.fetchone())[0] != 0:
                    return False
            continue
        m = re.search(r"ALTER TABLE\s+(\w+)\s+RENAME COLUMN\s+(\w+)\s+TO\s+(\w+)", stmt_up)
        if m:
            table, old_col = m.group(1).lower(), m.group(2).lower()
            async with db.execute(
                f"SELECT COUNT(*) FROM pragma_table_info('{table}') WHERE name=?", (old_col,)
            ) as cur:
                if (await cur.fetchone())[0] != 0:
                    return False
            continue
        m = re.search(r"CREATE INDEX\s+(?:IF NOT EXISTS\s+)?(\w+)", stmt_up)
        if m:
            async with db.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
                (m.group(1).lower(),),
            ) as cur:
                if not await cur.fetchone():
                    return False
            continue
        m = re.search(r"DROP TABLE\s+(?:IF EXISTS\s+)?(\w+)", stmt_up)
        if m:
            async with db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (m.group(1).lower(),),
            ) as cur:
                if await cur.fetchone():
                    return False
    return True


async def init_db() -> None:
    Path(settings.database_path).parent.mkdir(parents=True, exist_ok=True)
    migrations_dir = Path(__file__).parent / "migrations"
    async with aiosqlite.connect(settings.database_path) as db:
        await db.execute("PRAGMA journal_mode = WAL")
        await db.execute(
            "CREATE TABLE IF NOT EXISTS _migrations (name TEXT PRIMARY KEY)"
        )
        await db.commit()
        for migration in sorted(migrations_dir.glob("*.sql")):
            name = migration.name
            async with db.execute("SELECT 1 FROM _migrations WHERE name = ?", (name,)) as cur:
                if await cur.fetchone():
                    continue
            if await _schema_has_migration(db, migration):
                await db.execute("INSERT INTO _migrations (name) VALUES (?)", (name,))
                await db.commit()
                continue
            await db.executescript(migration.read_text())
            await db.execute("INSERT INTO _migrations (name) VALUES (?)", (name,))
            await db.commit()


async def get_app_setting(key: str) -> str | None:
    async with get_db() as db:
        async with db.execute("SELECT value FROM app_settings WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
            return row["value"] if row else None


async def set_app_setting(key: str, value: str) -> None:
    async with get_db() as db:
        await db.execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        await db.commit()

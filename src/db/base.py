"""Database foundation: the shared connection context manager, migration
runner and generic app-settings access. Domain modules (sources, items,
categories, blocked, radar) build on top of get_db()."""
import logging
import aiosqlite
from contextlib import asynccontextmanager
from pathlib import Path

from src.config import settings

log = logging.getLogger(__name__)


@asynccontextmanager
async def get_db():
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")
        yield db


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
    return True


async def init_db() -> None:
    Path(settings.database_path).parent.mkdir(parents=True, exist_ok=True)
    migrations_dir = Path(__file__).parent / "migrations"
    async with aiosqlite.connect(settings.database_path) as db:
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

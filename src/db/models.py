import aiosqlite
from pathlib import Path

from src.config import settings


async def get_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(settings.database_path)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA foreign_keys = ON")
    return db


async def init_db() -> None:
    Path(settings.database_path).parent.mkdir(parents=True, exist_ok=True)
    migration = Path(__file__).parent / "migrations" / "001_initial.sql"
    sql = migration.read_text()
    async with aiosqlite.connect(settings.database_path) as db:
        await db.executescript(sql)
        await db.commit()


async def source_exists(url: str) -> bool:
    async with await get_db() as db:
        async with db.execute("SELECT 1 FROM sources WHERE url = ?", (url,)) as cur:
            return await cur.fetchone() is not None


async def add_source(type_: str, name: str, url: str) -> int:
    async with await get_db() as db:
        cur = await db.execute(
            "INSERT INTO sources (type, name, url) VALUES (?, ?, ?)",
            (type_, name, url),
        )
        await db.commit()
        return cur.lastrowid


async def remove_source(source_id: int) -> bool:
    async with await get_db() as db:
        cur = await db.execute(
            "DELETE FROM sources WHERE id = ?", (source_id,)
        )
        await db.commit()
        return cur.rowcount > 0


async def get_active_sources(type_: str | None = None) -> list[aiosqlite.Row]:
    async with await get_db() as db:
        if type_:
            async with db.execute(
                "SELECT * FROM sources WHERE is_active = 1 AND type = ?", (type_,)
            ) as cur:
                return await cur.fetchall()
        async with db.execute("SELECT * FROM sources WHERE is_active = 1") as cur:
            return await cur.fetchall()


async def is_seen(message_id: str) -> bool:
    async with await get_db() as db:
        async with db.execute(
            "SELECT 1 FROM items WHERE message_id = ?", (message_id,)
        ) as cur:
            return await cur.fetchone() is not None


async def save_item(
    source_id: int,
    message_id: str,
    raw_text: str,
    original_url: str | None,
    published_at: str | None,
    summary: str | None,
    category: str | None,
    importance: str | None,
    processed_at: str,
) -> int:
    async with await get_db() as db:
        cur = await db.execute(
            """INSERT INTO items
               (source_id, message_id, raw_text, original_url, published_at,
                summary, category, importance, processed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (source_id, message_id, raw_text, original_url, published_at,
             summary, category, importance, processed_at),
        )
        await db.commit()
        return cur.lastrowid


async def get_unsent_items() -> list[aiosqlite.Row]:
    async with await get_db() as db:
        async with db.execute(
            "SELECT * FROM items WHERE sent = 0 ORDER BY published_at ASC"
        ) as cur:
            return await cur.fetchall()


async def mark_sent(item_ids: list[int]) -> None:
    async with await get_db() as db:
        await db.executemany(
            "UPDATE items SET sent = 1 WHERE id = ?",
            [(i,) for i in item_ids],
        )
        await db.commit()


async def get_categories() -> list[aiosqlite.Row]:
    async with await get_db() as db:
        async with db.execute(
            "SELECT * FROM categories WHERE is_active = 1 ORDER BY name"
        ) as cur:
            return await cur.fetchall()


async def add_category(name: str, emoji: str) -> int:
    async with await get_db() as db:
        cur = await db.execute(
            "INSERT INTO categories (name, emoji) VALUES (?, ?)", (name, emoji)
        )
        await db.commit()
        return cur.lastrowid


async def remove_category(name: str) -> bool:
    async with await get_db() as db:
        cur = await db.execute(
            "DELETE FROM categories WHERE name = ?", (name,)
        )
        await db.commit()
        return cur.rowcount > 0


async def set_category_topic(name: str, topic_id: int) -> None:
    async with await get_db() as db:
        await db.execute(
            "UPDATE categories SET topic_id = ? WHERE name = ?", (topic_id, name)
        )
        await db.commit()


async def log_digest(total: int, high: int, low: int, status: str = "ok") -> None:
    async with await get_db() as db:
        await db.execute(
            "INSERT INTO digest_log (items_total, items_high, items_low, status) VALUES (?, ?, ?, ?)",
            (total, high, low, status),
        )
        await db.commit()

import aiosqlite
from contextlib import asynccontextmanager
from pathlib import Path

from src.config import settings


@asynccontextmanager
async def get_db():
    async with aiosqlite.connect(settings.database_path) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA foreign_keys = ON")
        yield db


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
            await db.executescript(migration.read_text())
            await db.execute("INSERT INTO _migrations (name) VALUES (?)", (name,))
            await db.commit()


async def source_exists(url: str) -> bool:
    async with get_db() as db:
        async with db.execute("SELECT 1 FROM sources WHERE url = ?", (url,)) as cur:
            return await cur.fetchone() is not None


async def add_source(type_: str, name: str, url: str, category: str) -> int:
    async with get_db() as db:
        cur = await db.execute(
            "INSERT INTO sources (type, name, url, category) VALUES (?, ?, ?, ?)",
            (type_, name, url, category),
        )
        await db.commit()
        return cur.lastrowid


async def remove_source(source_id: int) -> bool:
    async with get_db() as db:
        await db.execute("DELETE FROM items WHERE source_id = ?", (source_id,))
        cur = await db.execute("DELETE FROM sources WHERE id = ?", (source_id,))
        await db.commit()
        return cur.rowcount > 0


async def get_active_sources(type_: str | None = None) -> list[aiosqlite.Row]:
    async with get_db() as db:
        if type_:
            async with db.execute(
                "SELECT * FROM sources WHERE is_active = 1 AND type = ?", (type_,)
            ) as cur:
                return await cur.fetchall()
        async with db.execute("SELECT * FROM sources WHERE is_active = 1") as cur:
            return await cur.fetchall()


async def is_seen(message_id: str) -> bool:
    async with get_db() as db:
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
    category: str,
    processed_at: str,
) -> int:
    async with get_db() as db:
        cur = await db.execute(
            """INSERT INTO items
               (source_id, message_id, raw_text, original_url, published_at,
                summary, category, processed_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (source_id, message_id, raw_text, original_url, published_at,
             summary, category, processed_at),
        )
        await db.commit()
        return cur.lastrowid


async def get_unsent_items(categories: list[str] | None = None) -> list[aiosqlite.Row]:
    async with get_db() as db:
        if categories:
            placeholders = ",".join("?" * len(categories))
            query = f"""SELECT items.*, sources.name AS source_name
               FROM items
               LEFT JOIN sources ON items.source_id = sources.id
               WHERE items.sent = 0 AND items.category IN ({placeholders})
               ORDER BY category, source_id, published_at ASC"""
            async with db.execute(query, categories) as cur:
                return await cur.fetchall()
        async with db.execute(
            """SELECT items.*, sources.name AS source_name
               FROM items
               LEFT JOIN sources ON items.source_id = sources.id
               WHERE items.sent = 0
               ORDER BY category, source_id, published_at ASC"""
        ) as cur:
            return await cur.fetchall()


async def mark_sent(item_ids: list[int]) -> None:
    async with get_db() as db:
        await db.executemany(
            "UPDATE items SET sent = 1 WHERE id = ?",
            [(i,) for i in item_ids],
        )
        await db.commit()


async def category_exists(name: str) -> bool:
    async with get_db() as db:
        async with db.execute("SELECT 1 FROM categories WHERE name = ?", (name,)) as cur:
            return await cur.fetchone() is not None


async def get_categories() -> list[aiosqlite.Row]:
    async with get_db() as db:
        async with db.execute(
            "SELECT * FROM categories WHERE is_active = 1 ORDER BY name"
        ) as cur:
            return await cur.fetchall()


async def add_category(name: str, emoji: str, digest_time: str = "21:00") -> int:
    async with get_db() as db:
        cur = await db.execute(
            "INSERT INTO categories (name, emoji, digest_time) VALUES (?, ?, ?)",
            (name, emoji, digest_time),
        )
        await db.commit()
        return cur.lastrowid


async def update_category(
    old_name: str,
    new_name: str | None = None,
    new_emoji: str | None = None,
    new_digest_time: str | None = None,
) -> bool:
    async with get_db() as db:
        async with db.execute("SELECT * FROM categories WHERE name = ?", (old_name,)) as cur:
            cat = await cur.fetchone()
        if not cat:
            return False
        name = new_name if new_name is not None else cat["name"]
        emoji = new_emoji if new_emoji is not None else cat["emoji"]
        digest_time = new_digest_time if new_digest_time is not None else cat["digest_time"]
        await db.execute(
            "UPDATE categories SET name = ?, emoji = ?, digest_time = ? WHERE name = ?",
            (name, emoji, digest_time, old_name),
        )
        if name != old_name:
            await db.execute("UPDATE sources SET category = ? WHERE category = ?", (name, old_name))
            await db.execute("UPDATE items SET category = ? WHERE category = ?", (name, old_name))
        await db.commit()
        return True


async def get_sources_by_category(cat_name: str) -> list[aiosqlite.Row]:
    async with get_db() as db:
        async with db.execute(
            "SELECT * FROM sources WHERE category = ? AND is_active = 1", (cat_name,)
        ) as cur:
            return await cur.fetchall()


async def move_sources_to_category(from_cat: str, to_cat: str) -> None:
    async with get_db() as db:
        await db.execute("UPDATE sources SET category = ? WHERE category = ?", (to_cat, from_cat))
        await db.execute("UPDATE items SET category = ? WHERE category = ?", (to_cat, from_cat))
        await db.commit()


async def delete_sources_by_category(cat_name: str) -> None:
    async with get_db() as db:
        async with db.execute(
            "SELECT id FROM sources WHERE category = ?", (cat_name,)
        ) as cur:
            source_ids = [r[0] for r in await cur.fetchall()]
        for sid in source_ids:
            await db.execute("DELETE FROM items WHERE source_id = ?", (sid,))
        await db.execute("DELETE FROM sources WHERE category = ?", (cat_name,))
        await db.commit()


async def remove_category(name: str) -> bool:
    async with get_db() as db:
        cur = await db.execute("DELETE FROM categories WHERE name = ?", (name,))
        await db.commit()
        return cur.rowcount > 0


async def log_digest(total: int, high: int, low: int, status: str = "ok") -> None:
    async with get_db() as db:
        await db.execute(
            "INSERT INTO digest_log (items_total, items_high, items_low, status) VALUES (?, ?, ?, ?)",
            (total, high, low, status),
        )
        await db.commit()

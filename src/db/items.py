"""Collected items: dedup lookup, insertion, the unsent queue feeding the
digest, classification backfill helpers and the digest log."""
import aiosqlite

from src.db.base import get_db


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
    key_phrase: str | None = None,
) -> int:
    async with get_db() as db:
        cur = await db.execute(
            """INSERT INTO items
               (source_id, message_id, raw_text, original_url, published_at,
                summary, category, processed_at, key_phrase)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (source_id, message_id, raw_text, original_url, published_at,
             summary, category, processed_at, key_phrase or None),
        )
        await db.commit()
        return cur.lastrowid


async def get_unsent_items(categories: list[str] | None = None) -> list[aiosqlite.Row]:
    async with get_db() as db:
        if categories:
            placeholders = ",".join("?" * len(categories))
            query = f"""SELECT items.*, sources.name AS source_name, sources.prompt_extra AS source_prompt_extra
               FROM items
               LEFT JOIN sources ON items.source_id = sources.id
               WHERE items.sent = 0 AND items.category IN ({placeholders})
               ORDER BY category, sources.sort_order ASC, source_id, published_at ASC"""
            async with db.execute(query, categories) as cur:
                return await cur.fetchall()
        async with db.execute(
            """SELECT items.*, sources.name AS source_name, sources.prompt_extra AS source_prompt_extra
               FROM items
               LEFT JOIN sources ON items.source_id = sources.id
               WHERE items.sent = 0
               ORDER BY category, sources.sort_order ASC, source_id, published_at ASC"""
        ) as cur:
            return await cur.fetchall()


async def get_sent_empty_items(limit: int = 5) -> list[aiosqlite.Row]:
    async with get_db() as db:
        async with db.execute(
            """SELECT * FROM items
               WHERE sent = 1
                 AND (summary IS NULL OR trim(summary) = '')
                 AND trim(raw_text) <> ''
               ORDER BY id DESC LIMIT ?""",
            (limit,),
        ) as cur:
            return await cur.fetchall()


async def update_item_classification(item_id: int, summary: str, key_phrase: str) -> None:
    async with get_db() as db:
        await db.execute(
            "UPDATE items SET summary = ?, key_phrase = ? WHERE id = ?",
            (summary, key_phrase, item_id),
        )
        await db.commit()


async def increment_classify_attempts(item_id: int) -> int:
    async with get_db() as db:
        await db.execute(
            "UPDATE items SET classify_attempts = classify_attempts + 1 WHERE id = ?",
            (item_id,),
        )
        await db.commit()
        async with db.execute("SELECT classify_attempts FROM items WHERE id = ?", (item_id,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def mark_sent(item_ids: list[int]) -> None:
    async with get_db() as db:
        await db.executemany(
            "UPDATE items SET sent = 1 WHERE id = ?",
            [(i,) for i in item_ids],
        )
        await db.commit()


async def log_digest(total: int, status: str = "ok") -> None:
    async with get_db() as db:
        await db.execute(
            "INSERT INTO digest_log (items_total, status) VALUES (?, ?)",
            (total, status),
        )
        await db.commit()

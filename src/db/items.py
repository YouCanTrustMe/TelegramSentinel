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
            query = f"""SELECT items.*, sources.name AS source_name, sources.prompt_extra AS source_prompt_extra,
                      sources.sort_order AS source_sort_order
               FROM items
               LEFT JOIN sources ON items.source_id = sources.id
               WHERE items.sent = 0 AND items.category IN ({placeholders})
               ORDER BY category, sources.sort_order ASC, source_id, published_at ASC"""
            async with db.execute(query, categories) as cur:
                return await cur.fetchall()
        async with db.execute(
            """SELECT items.*, sources.name AS source_name, sources.prompt_extra AS source_prompt_extra,
                  sources.sort_order AS source_sort_order
               FROM items
               LEFT JOIN sources ON items.source_id = sources.id
               WHERE items.sent = 0
               ORDER BY category, sources.sort_order ASC, source_id, published_at ASC"""
        ) as cur:
            return await cur.fetchall()


async def set_item_embeddings(pairs: list[tuple[int, bytes]]) -> None:
    """Store many item embeddings in one connection/commit (called per digest for
    every freshly embedded item)."""
    if not pairs:
        return
    async with get_db() as db:
        await db.executemany(
            "UPDATE items SET embedding = ? WHERE id = ?",
            [(blob, item_id) for item_id, blob in pairs],
        )
        await db.commit()


async def mark_duplicate(item_id: int, primary_id: int) -> None:
    """Mute a cross-source duplicate: point it at its primary and mark it sent so
    it never enters a digest on its own. The row is kept so the digest can render
    a link to it under the surviving primary."""
    async with get_db() as db:
        await db.execute(
            "UPDATE items SET duplicate_of = ?, sent = 1 WHERE id = ?",
            (primary_id, item_id),
        )
        await db.commit()


async def get_recent_embedded_items(window_hours: int) -> list[aiosqlite.Row]:
    """Items with a stored embedding within the window (sent and unsent), used as
    the comparison pool for cross-source dedup — lets a new item match one already
    sent in a previous digest, not only items in the current batch."""
    async with get_db() as db:
        async with db.execute(
            """SELECT items.id, items.category, items.source_id, items.published_at,
                      items.sent, items.embedding, items.summary,
                      sources.sort_order AS source_sort_order
               FROM items
               LEFT JOIN sources ON items.source_id = sources.id
               WHERE items.embedding IS NOT NULL
                 AND julianday(items.processed_at) >= julianday('now', ?)""",
            (f"-{window_hours} hours",),
        ) as cur:
            return await cur.fetchall()


async def get_duplicate_links(primary_ids: list[int]) -> dict[int, list[tuple[str, str]]]:
    """For each primary id, the (source_name, original_url) of duplicates muted
    under it — rendered as links beside the surviving item."""
    if not primary_ids:
        return {}
    placeholders = ",".join("?" * len(primary_ids))
    async with get_db() as db:
        async with db.execute(
            f"""SELECT items.duplicate_of AS primary_id, sources.name AS source_name,
                       items.original_url AS original_url
                FROM items
                LEFT JOIN sources ON items.source_id = sources.id
                WHERE items.duplicate_of IN ({placeholders})
                ORDER BY sources.sort_order ASC, items.id ASC""",
            primary_ids,
        ) as cur:
            rows = await cur.fetchall()
    out: dict[int, list[tuple[str, str]]] = {}
    for row in rows:
        out.setdefault(row["primary_id"], []).append(
            (row["source_name"] or "?", row["original_url"] or "")
        )
    return out


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


async def prune_old_items(retention_days: int) -> int:
    """Delete already-sent items older than retention_days to bound table
    growth. Trade-off: RSS dedup relies on these rows (is_seen checks
    message_id), so an entry a feed still serves past the window can re-surface;
    Telegram sources are protected by last_message_id instead."""
    async with get_db() as db:
        cur = await db.execute(
            "DELETE FROM items WHERE sent = 1 AND julianday(processed_at) < julianday('now', ?)",
            (f"-{retention_days} days",),
        )
        await db.commit()
        return cur.rowcount

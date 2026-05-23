"""Source rows: CRUD, status/fail-count tracking, ordering within a category
and silence detection."""
import logging
import aiosqlite

from src.db.base import get_db

log = logging.getLogger(__name__)


async def source_exists(url: str) -> bool:
    async with get_db() as db:
        async with db.execute("SELECT 1 FROM sources WHERE url = ?", (url,)) as cur:
            return await cur.fetchone() is not None


async def add_source(type_: str, name: str, url: str, category: str, status: str = "active") -> int:
    async with get_db() as db:
        cur = await db.execute(
            "INSERT INTO sources (type, name, url, category, status) VALUES (?, ?, ?, ?, ?)",
            (type_, name, url, category, status),
        )
        await db.commit()
        return cur.lastrowid


async def get_pending_sources(category: str | None = None) -> list[aiosqlite.Row]:
    async with get_db() as db:
        if category:
            async with db.execute(
                "SELECT * FROM sources WHERE status = 'pending' AND category = ?", (category,)
            ) as cur:
                return await cur.fetchall()
        async with db.execute("SELECT * FROM sources WHERE status = 'pending'") as cur:
            return await cur.fetchall()


async def set_source_pending_msg_id(source_id: int, msg_id: int | None) -> None:
    async with get_db() as db:
        await db.execute("UPDATE sources SET pending_msg_id = ? WHERE id = ?", (msg_id, source_id))
        await db.commit()


async def set_source_last_message_id(source_id: int, msg_id: int) -> None:
    async with get_db() as db:
        await db.execute(
            "UPDATE sources SET last_message_id = ? WHERE id = ? AND (last_message_id IS NULL OR last_message_id < ?)",
            (msg_id, source_id, msg_id),
        )
        await db.commit()


async def rename_source(source_id: int, new_name: str) -> bool:
    async with get_db() as db:
        cur = await db.execute(
            "UPDATE sources SET name = ? WHERE id = ?", (new_name, source_id)
        )
        await db.commit()
        return cur.rowcount > 0


async def activate_source(source_id: int) -> bool:
    async with get_db() as db:
        cur = await db.execute(
            "UPDATE sources SET status = 'active' WHERE id = ?", (source_id,)
        )
        await db.commit()
        return cur.rowcount > 0


async def update_source_url(source_id: int, url: str) -> None:
    async with get_db() as db:
        await db.execute("UPDATE sources SET url = ? WHERE id = ?", (url, source_id))
        await db.commit()


async def set_source_chat_id(source_id: int, chat_id: int) -> None:
    async with get_db() as db:
        await db.execute(
            "UPDATE sources SET chat_id = ? WHERE id = ? AND (chat_id IS NULL OR chat_id != ?)",
            (chat_id, source_id, chat_id),
        )
        await db.commit()


async def find_sources_by_chat_id(chat_id: int, exclude_id: int | None = None) -> list[aiosqlite.Row]:
    async with get_db() as db:
        if exclude_id is None:
            async with db.execute(
                "SELECT * FROM sources WHERE chat_id = ?", (chat_id,)
            ) as cur:
                return await cur.fetchall()
        async with db.execute(
            "SELECT * FROM sources WHERE chat_id = ? AND id != ?", (chat_id, exclude_id)
        ) as cur:
            return await cur.fetchall()


async def increment_source_fail_count(source_id: int) -> int:
    async with get_db() as db:
        await db.execute("UPDATE sources SET fail_count = fail_count + 1 WHERE id = ?", (source_id,))
        await db.commit()
        async with db.execute("SELECT fail_count FROM sources WHERE id = ?", (source_id,)) as cur:
            row = await cur.fetchone()
            return int(row["fail_count"]) if row else 0


async def reset_source_fail_count(source_id: int) -> None:
    async with get_db() as db:
        await db.execute("UPDATE sources SET fail_count = 0 WHERE id = ?", (source_id,))
        await db.commit()


async def update_source_status(source_id: int, status: str) -> None:
    async with get_db() as db:
        await db.execute("UPDATE sources SET status = ? WHERE id = ?", (status, source_id))
        await db.commit()
    log.info("Source id=%d status → %s", source_id, status)


async def remove_source(source_id: int) -> bool:
    async with get_db() as db:
        await db.execute("DELETE FROM items WHERE source_id = ?", (source_id,))
        cur = await db.execute("DELETE FROM sources WHERE id = ?", (source_id,))
        await db.commit()
        return cur.rowcount > 0


async def get_source(source_id: int) -> aiosqlite.Row | None:
    async with get_db() as db:
        async with db.execute("SELECT * FROM sources WHERE id = ?", (source_id,)) as cur:
            return await cur.fetchone()


async def get_active_sources(type_: str | None = None) -> list[aiosqlite.Row]:
    async with get_db() as db:
        if type_:
            async with db.execute(
                "SELECT * FROM sources WHERE status = 'active' AND type = ? ORDER BY sort_order ASC, name ASC", (type_,)
            ) as cur:
                return await cur.fetchall()
        async with db.execute(
            "SELECT * FROM sources WHERE status = 'active' ORDER BY sort_order ASC, name ASC"
        ) as cur:
            return await cur.fetchall()


async def get_sources_by_category(cat_name: str) -> list[aiosqlite.Row]:
    async with get_db() as db:
        async with db.execute(
            "SELECT * FROM sources WHERE category = ? AND status = 'active' ORDER BY sort_order ASC, name ASC", (cat_name,)
        ) as cur:
            return await cur.fetchall()


async def reassign_source_category(source_id: int, new_cat: str) -> None:
    async with get_db() as db:
        await db.execute("UPDATE sources SET category = ? WHERE id = ?", (new_cat, source_id))
        await db.execute(
            "UPDATE items SET category = ? WHERE source_id = ? AND sent = 0",
            (new_cat, source_id),
        )
        await db.commit()


async def place_source_at_bottom(source_id: int, cat_name: str) -> None:
    async with get_db() as db:
        async with db.execute(
            "SELECT COALESCE(MAX(sort_order), -1) FROM sources WHERE category = ? AND id != ?",
            (cat_name, source_id),
        ) as cur:
            max_order = (await cur.fetchone())[0]
        await db.execute("UPDATE sources SET sort_order = ? WHERE id = ?", (max_order + 1, source_id))
        await db.commit()


async def set_source_prompt_extra(source_id: int, text: str | None) -> None:
    async with get_db() as db:
        await db.execute("UPDATE sources SET prompt_extra = ? WHERE id = ?", (text, source_id))
        await db.commit()


async def get_silent_sources(threshold_hours: int = 120) -> list[aiosqlite.Row]:
    async with get_db() as db:
        async with db.execute(
            """SELECT s.id, s.name, s.type,
                      MAX(i.processed_at) AS last_item_at,
                      CAST((julianday('now') - julianday(MAX(i.processed_at))) * 24 AS INTEGER) AS hours_silent
               FROM sources s
               LEFT JOIN items i ON i.source_id = s.id
               WHERE s.status = 'active'
               GROUP BY s.id
               HAVING last_item_at IS NULL
                  OR last_item_at < datetime('now', ?)
               ORDER BY last_item_at ASC""",
            (f"-{threshold_hours} hours",),
        ) as cur:
            return await cur.fetchall()


async def reorder_source(source_id: int, cat_name: str, direction: str) -> None:
    async with get_db() as db:
        async with db.execute(
            "SELECT id FROM sources WHERE category = ? ORDER BY sort_order ASC, name ASC", (cat_name,)
        ) as cur:
            rows = await cur.fetchall()
        ids = [r["id"] for r in rows]
        idx = next((i for i, sid in enumerate(ids) if sid == source_id), None)
        if idx is None:
            return
        swap_idx = idx - 1 if direction == "up" else idx + 1
        if not (0 <= swap_idx < len(ids)):
            return
        ids[idx], ids[swap_idx] = ids[swap_idx], ids[idx]
        for i, sid in enumerate(ids):
            await db.execute("UPDATE sources SET sort_order = ? WHERE id = ?", (i, sid))
        await db.commit()

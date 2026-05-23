"""Radar feature storage: keywords, monitored chats, keyword↔chat links,
user blacklist, the alert log and chat-silence tracking."""
import aiosqlite

from src.db.base import get_db


async def get_radar_keywords() -> list[aiosqlite.Row]:
    async with get_db() as db:
        async with db.execute("SELECT * FROM radar_keywords ORDER BY keyword") as cur:
            return await cur.fetchall()


async def add_radar_keyword(keyword: str) -> bool:
    async with get_db() as db:
        try:
            await db.execute("INSERT INTO radar_keywords (keyword) VALUES (?)", (keyword,))
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False


async def remove_radar_keyword(keyword_id: int) -> bool:
    async with get_db() as db:
        cur = await db.execute("DELETE FROM radar_keywords WHERE id = ?", (keyword_id,))
        await db.commit()
        return cur.rowcount > 0


async def get_radar_chats() -> list[aiosqlite.Row]:
    async with get_db() as db:
        async with db.execute("SELECT * FROM radar_chats ORDER BY id") as cur:
            return await cur.fetchall()


async def add_radar_chat(chat_ref: str, title: str | None, chat_id: int | None = None) -> bool:
    async with get_db() as db:
        try:
            await db.execute(
                "INSERT INTO radar_chats (chat_ref, title, chat_id) VALUES (?, ?, ?)",
                (chat_ref, title, chat_id),
            )
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False


async def remove_radar_chat(chat_id: int) -> bool:
    async with get_db() as db:
        cur = await db.execute("DELETE FROM radar_chats WHERE id = ?", (chat_id,))
        await db.commit()
        return cur.rowcount > 0


async def update_radar_chat_status(entry_id: int, status: str) -> None:
    async with get_db() as db:
        await db.execute(
            "UPDATE radar_chats SET status = ?, last_verified_at = datetime('now') WHERE id = ?",
            (status, entry_id),
        )
        await db.commit()


async def update_radar_chat_resolved(entry_id: int, chat_id: int, chat_ref: str, title: str | None) -> None:
    async with get_db() as db:
        await db.execute(
            "UPDATE radar_chats SET chat_id = ?, chat_ref = ?, title = COALESCE(?, title), "
            "status = 'active', last_verified_at = datetime('now') WHERE id = ?",
            (chat_id, chat_ref, title, entry_id),
        )
        await db.commit()


async def get_radar_blacklist() -> list[aiosqlite.Row]:
    async with get_db() as db:
        async with db.execute("SELECT * FROM radar_blacklist ORDER BY id") as cur:
            return await cur.fetchall()


async def add_radar_blacklist(user_id: int) -> bool:
    async with get_db() as db:
        try:
            await db.execute("INSERT INTO radar_blacklist (user_id) VALUES (?)", (user_id,))
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False


async def remove_radar_blacklist(entry_id: int) -> bool:
    async with get_db() as db:
        cur = await db.execute("DELETE FROM radar_blacklist WHERE id = ?", (entry_id,))
        await db.commit()
        return cur.rowcount > 0


async def log_radar_alert(
    keyword: str,
    chat_ref: str,
    author_id: int | None,
    message_text: str,
    message_url: str,
) -> None:
    async with get_db() as db:
        await db.execute(
            "INSERT INTO radar_alert_log (keyword, chat_ref, author_id, message_text, message_url)"
            " VALUES (?, ?, ?, ?, ?)",
            (keyword, chat_ref, author_id, message_text, message_url),
        )
        await db.commit()


async def get_recent_radar_alerts(limit: int = 3) -> list[aiosqlite.Row]:
    async with get_db() as db:
        async with db.execute(
            "SELECT * FROM radar_alert_log ORDER BY id DESC LIMIT ?", (limit,)
        ) as cur:
            return await cur.fetchall()


async def link_keyword_chat(keyword_id: int, chat_id: int) -> bool:
    async with get_db() as db:
        try:
            await db.execute(
                "INSERT INTO radar_keyword_chats (keyword_id, chat_id) VALUES (?, ?)",
                (keyword_id, chat_id),
            )
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False


async def unlink_keyword_chat(keyword_id: int, chat_id: int) -> bool:
    async with get_db() as db:
        cur = await db.execute(
            "DELETE FROM radar_keyword_chats WHERE keyword_id = ? AND chat_id = ?",
            (keyword_id, chat_id),
        )
        await db.commit()
        return cur.rowcount > 0


async def get_keyword_chat_links() -> list[aiosqlite.Row]:
    async with get_db() as db:
        async with db.execute(
            "SELECT keyword_id, chat_id FROM radar_keyword_chats"
        ) as cur:
            return await cur.fetchall()


async def get_chats_for_keyword(keyword_id: int) -> list[aiosqlite.Row]:
    async with get_db() as db:
        async with db.execute(
            "SELECT c.* FROM radar_chats c "
            "JOIN radar_keyword_chats l ON l.chat_id = c.id "
            "WHERE l.keyword_id = ? ORDER BY c.id",
            (keyword_id,),
        ) as cur:
            return await cur.fetchall()


async def get_keyword_ids_for_chat(chat_id: int) -> set[int]:
    async with get_db() as db:
        async with db.execute(
            "SELECT keyword_id FROM radar_keyword_chats WHERE chat_id = ?",
            (chat_id,),
        ) as cur:
            rows = await cur.fetchall()
            return {r["keyword_id"] for r in rows}


async def count_links_for_keyword(keyword_id: int) -> int:
    async with get_db() as db:
        async with db.execute(
            "SELECT COUNT(*) AS n FROM radar_keyword_chats WHERE keyword_id = ?",
            (keyword_id,),
        ) as cur:
            row = await cur.fetchone()
            return int(row["n"]) if row else 0


async def count_links_for_chat(chat_id: int) -> int:
    async with get_db() as db:
        async with db.execute(
            "SELECT COUNT(*) AS n FROM radar_keyword_chats WHERE chat_id = ?",
            (chat_id,),
        ) as cur:
            row = await cur.fetchone()
            return int(row["n"]) if row else 0


async def get_silent_radar_chats(threshold_hours: int = 120) -> list[aiosqlite.Row]:
    async with get_db() as db:
        async with db.execute(
            """SELECT id, chat_ref, title, last_message_at,
                      CAST((julianday('now') - julianday(last_message_at)) * 24 AS INTEGER) AS hours_silent
               FROM radar_chats
               WHERE status = 'active'
                 AND last_message_at IS NOT NULL
                 AND last_message_at < datetime('now', ?)
               ORDER BY last_message_at ASC""",
            (f"-{threshold_hours} hours",),
        ) as cur:
            return await cur.fetchall()


async def update_radar_last_message_at(entry_id: int) -> None:
    async with get_db() as db:
        await db.execute(
            "UPDATE radar_chats SET last_message_at = datetime('now') WHERE id = ?",
            (entry_id,),
        )
        await db.commit()

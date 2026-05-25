"""Semantic filter rules used to exclude unwanted items from digests."""
import aiosqlite

from src.db.base import get_db


async def get_blocked_words() -> list[aiosqlite.Row]:
    async with get_db() as db:
        async with db.execute("SELECT * FROM blocked_words ORDER BY rule") as cur:
            return await cur.fetchall()


async def add_blocked_word(word: str) -> bool:
    async with get_db() as db:
        try:
            await db.execute("INSERT INTO blocked_words (rule) VALUES (?)", (word,))
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False


async def remove_blocked_word(word_id: int) -> bool:
    async with get_db() as db:
        cur = await db.execute("DELETE FROM blocked_words WHERE id = ?", (word_id,))
        await db.commit()
        return cur.rowcount > 0

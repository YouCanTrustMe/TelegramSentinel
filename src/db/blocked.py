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


async def set_blocked_label(word_id: int, label: str | None) -> bool:
    async with get_db() as db:
        cur = await db.execute("UPDATE blocked_words SET label = ? WHERE id = ?", (label, word_id))
        await db.commit()
        return cur.rowcount > 0


async def remove_blocked_word(word_id: int) -> bool:
    async with get_db() as db:
        await db.execute("DELETE FROM blocked_word_categories WHERE word_id = ?", (word_id,))
        await db.execute("DELETE FROM blocked_word_sources WHERE word_id = ?", (word_id,))
        cur = await db.execute("DELETE FROM blocked_words WHERE id = ?", (word_id,))
        await db.commit()
        return cur.rowcount > 0


async def get_categories_for_word(word_id: int) -> list[str]:
    async with get_db() as db:
        async with db.execute(
            "SELECT category FROM blocked_word_categories WHERE word_id = ? ORDER BY category", (word_id,)
        ) as cur:
            return [row["category"] for row in await cur.fetchall()]


async def link_word_category(word_id: int, category: str) -> None:
    async with get_db() as db:
        await db.execute(
            "INSERT OR IGNORE INTO blocked_word_categories (word_id, category) VALUES (?, ?)",
            (word_id, category),
        )
        await db.commit()


async def unlink_word_category(word_id: int, category: str) -> None:
    async with get_db() as db:
        await db.execute(
            "DELETE FROM blocked_word_categories WHERE word_id = ? AND category = ?",
            (word_id, category),
        )
        await db.commit()


async def get_word_category_map() -> dict[int, set[str]]:
    """Return {word_id: {categories}} for every rule that has an explicit scope.
    A rule absent from the map applies to all categories (empty scope = all)."""
    async with get_db() as db:
        async with db.execute("SELECT word_id, category FROM blocked_word_categories") as cur:
            rows = await cur.fetchall()
    scope: dict[int, set[str]] = {}
    for row in rows:
        scope.setdefault(row["word_id"], set()).add(row["category"])
    return scope


async def get_sources_for_word(word_id: int) -> list[int]:
    async with get_db() as db:
        async with db.execute(
            "SELECT source_id FROM blocked_word_sources WHERE word_id = ? ORDER BY source_id", (word_id,)
        ) as cur:
            return [row["source_id"] for row in await cur.fetchall()]


async def toggle_word_source(word_id: int, source_id: int) -> bool:
    """Add the source to the rule's own list, or take it off. True when it is now on."""
    async with get_db() as db:
        cur = await db.execute(
            "DELETE FROM blocked_word_sources WHERE word_id = ? AND source_id = ?", (word_id, source_id)
        )
        if not cur.rowcount:
            await db.execute(
                "INSERT INTO blocked_word_sources (word_id, source_id) VALUES (?, ?)", (word_id, source_id)
            )
        await db.commit()
        return not cur.rowcount


async def get_word_source_map() -> dict[int, set[int]]:
    """Return {word_id: {source ids}} for every rule limited to some sources; a rule
    absent from the map applies to every source of its categories. Ids, not names:
    two sources may share a name."""
    async with get_db() as db:
        async with db.execute("SELECT word_id, source_id FROM blocked_word_sources") as cur:
            rows = await cur.fetchall()
    out: dict[int, set[int]] = {}
    for row in rows:
        out.setdefault(row["word_id"], set()).add(row["source_id"])
    return out


async def get_blocked_hit_counts(days: int = 30) -> dict[str, int]:
    """How often each rule blocked something recently. A rule at 0 is dead weight;
    the rule with the highest count is the first place to look when an item that
    should have been in the digest is missing."""
    async with get_db() as db:
        async with db.execute(
            "SELECT blocked_reason AS rule, COUNT(*) AS n FROM items "
            "WHERE blocked_reason IS NOT NULL "
            "  AND julianday(processed_at) >= julianday('now', ?) "
            "GROUP BY blocked_reason",
            (f"-{days} days",),
        ) as cur:
            return {row["rule"]: row["n"] for row in await cur.fetchall()}

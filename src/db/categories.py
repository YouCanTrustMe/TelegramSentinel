"""Category rows: CRUD, ordering, cascade rename/move/delete of their sources
and items, and bulk prompt-extra assignment."""
import aiosqlite

from src.db.base import get_db


async def category_exists(name: str) -> bool:
    async with get_db() as db:
        async with db.execute("SELECT 1 FROM categories WHERE name = ?", (name,)) as cur:
            return await cur.fetchone() is not None


async def get_categories() -> list[aiosqlite.Row]:
    async with get_db() as db:
        async with db.execute(
            "SELECT * FROM categories WHERE is_active = 1 ORDER BY sort_order ASC, name ASC"
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
            # Filter-rule scope keys on category name; cascade the rename or it orphans.
            await db.execute("UPDATE OR IGNORE blocked_word_categories SET category = ? WHERE category = ?", (name, old_name))
            await db.execute("DELETE FROM blocked_word_categories WHERE category = ?", (old_name,))
        await db.commit()
        return True


async def move_sources_to_category(from_cat: str, to_cat: str) -> None:
    async with get_db() as db:
        await db.execute("UPDATE sources SET category = ? WHERE category = ?", (to_cat, from_cat))
        await db.execute("UPDATE items SET category = ? WHERE category = ?", (to_cat, from_cat))
        await db.execute("UPDATE OR IGNORE blocked_word_categories SET category = ? WHERE category = ?", (to_cat, from_cat))
        await db.execute("DELETE FROM blocked_word_categories WHERE category = ?", (from_cat,))
        await db.commit()


async def delete_sources_by_category(cat_name: str) -> None:
    async with get_db() as db:
        await db.execute(
            "DELETE FROM items WHERE source_id IN (SELECT id FROM sources WHERE category = ?)",
            (cat_name,),
        )
        await db.execute("DELETE FROM sources WHERE category = ?", (cat_name,))
        await db.execute("DELETE FROM blocked_word_categories WHERE category = ?", (cat_name,))
        await db.commit()


async def remove_category(name: str) -> bool:
    async with get_db() as db:
        cur = await db.execute("DELETE FROM categories WHERE name = ?", (name,))
        await db.execute("DELETE FROM blocked_word_categories WHERE category = ?", (name,))
        await db.commit()
        return cur.rowcount > 0


async def reorder_category(name: str, direction: str) -> None:
    async with get_db() as db:
        async with db.execute(
            "SELECT name FROM categories WHERE is_active = 1 ORDER BY sort_order ASC, name ASC"
        ) as cur:
            rows = await cur.fetchall()
        names = [r["name"] for r in rows]
        idx = next((i for i, n in enumerate(names) if n == name), None)
        if idx is None:
            return
        swap_idx = idx - 1 if direction == "up" else idx + 1
        if not (0 <= swap_idx < len(names)):
            return
        names[idx], names[swap_idx] = names[swap_idx], names[idx]
        for i, n in enumerate(names):
            await db.execute("UPDATE categories SET sort_order = ? WHERE name = ?", (i, n))
        await db.commit()


async def bulk_set_category_prompt_extra(cat_name: str, text: str | None) -> int:
    async with get_db() as db:
        cur = await db.execute(
            "UPDATE sources SET prompt_extra = ? WHERE category = ? AND status = 'active'",
            (text, cat_name),
        )
        await db.commit()
        return cur.rowcount

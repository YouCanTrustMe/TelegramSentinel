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
        await db.commit()
        return True


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


async def move_sources_to_category(from_cat: str, to_cat: str) -> None:
    async with get_db() as db:
        await db.execute("UPDATE sources SET category = ? WHERE category = ?", (to_cat, from_cat))
        await db.execute("UPDATE items SET category = ? WHERE category = ?", (to_cat, from_cat))
        await db.commit()


async def delete_sources_by_category(cat_name: str) -> None:
    async with get_db() as db:
        await db.execute(
            "DELETE FROM items WHERE source_id IN (SELECT id FROM sources WHERE category = ?)",
            (cat_name,),
        )
        await db.execute("DELETE FROM sources WHERE category = ?", (cat_name,))
        await db.commit()


async def remove_category(name: str) -> bool:
    async with get_db() as db:
        cur = await db.execute("DELETE FROM categories WHERE name = ?", (name,))
        await db.commit()
        return cur.rowcount > 0


async def log_digest(total: int, status: str = "ok") -> None:
    async with get_db() as db:
        await db.execute(
            "INSERT INTO digest_log (items_total, status) VALUES (?, ?)",
            (total, status),
        )
        await db.commit()


async def get_blocked_words() -> list[aiosqlite.Row]:
    async with get_db() as db:
        async with db.execute("SELECT * FROM blocked_words ORDER BY word") as cur:
            return await cur.fetchall()


async def add_blocked_word(word: str) -> bool:
    async with get_db() as db:
        try:
            await db.execute("INSERT INTO blocked_words (word) VALUES (?)", (word.lower(),))
            await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False


async def remove_blocked_word(word_id: int) -> bool:
    async with get_db() as db:
        cur = await db.execute("DELETE FROM blocked_words WHERE id = ?", (word_id,))
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


async def bulk_set_category_prompt_extra(cat_name: str, text: str | None) -> int:
    async with get_db() as db:
        cur = await db.execute(
            "UPDATE sources SET prompt_extra = ? WHERE category = ? AND status = 'active'",
            (text, cat_name),
        )
        await db.commit()
        return cur.rowcount


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

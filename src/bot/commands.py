import logging

from pyrogram import filters
from pyrogram.types import Message

from src.collectors.telegram_collector import load_watched_channels
from src.config import settings
from src.db.models import (
    add_category,
    add_source,
    get_active_sources,
    get_categories,
    remove_category,
    remove_source,
    source_exists,
)
from src.dispatcher.digest_builder import send_digest
from src.dispatcher.sender import bot

log = logging.getLogger(__name__)


def _is_rss(url: str) -> bool:
    return url.startswith("http://") or url.startswith("https://")


def register_commands() -> None:
    admin = filters.user(settings.telegram_admin_id) & filters.private

    @bot.on_message(filters.private)
    async def cmd_debug_all(_, message: Message) -> None:
        log.info("DEBUG incoming: user_id=%s text=%r", message.from_user.id if message.from_user else None, message.text)

    @bot.on_message(filters.command("add_source") & admin)
    async def cmd_add_source(_, message: Message) -> None:
        parts = message.text.split(maxsplit=3)
        if len(parts) < 4:
            await message.reply("Usage: /add_source <name> <url or @channel> <category>")
            return

        name, url, category = parts[1], parts[2], parts[3].lower()
        source_type = "rss" if _is_rss(url) else "telegram"

        if await source_exists(url):
            await message.reply(f"Source <code>{url}</code> already exists.")
            return

        source_id = await add_source(source_type, name, url, category)
        if source_type == "telegram":
            await load_watched_channels()

        log.info("Source added: [%s] %s (%s) -> category=%s", source_type, name, url, category)
        await message.reply(
            f"✅ Added [{source_type}] <b>{name}</b> — <code>{url}</code>\n"
            f"Category: <b>{category}</b> | ID: <code>{source_id}</code>"
        )

    @bot.on_message(filters.command("remove_source") & admin)
    async def cmd_remove_source(_, message: Message) -> None:
        parts = message.text.split()
        if len(parts) < 2 or not parts[1].isdigit():
            await message.reply("Usage: /remove_source <id>")
            return

        removed = await remove_source(int(parts[1]))
        if removed:
            await load_watched_channels()
            log.info("Source removed: id=%s", parts[1])
            await message.reply("✅ Source removed.")
        else:
            await message.reply("Source not found.")

    @bot.on_message(filters.command("list_sources") & admin)
    async def cmd_list_sources(_, message: Message) -> None:
        sources = await get_active_sources()
        if not sources:
            await message.reply("No sources configured.")
            return
        lines = [
            f"<code>{r['id']}</code> [{r['type']}] <b>{r['name']}</b> [{r['category']}] — {r['url']}"
            for r in sources
        ]
        await message.reply("\n".join(lines))

    @bot.on_message(filters.command("add_category") & admin)
    async def cmd_add_category(_, message: Message) -> None:
        parts = message.text.split(maxsplit=2)
        if len(parts) < 3:
            await message.reply("Usage: /add_category <name> <emoji>")
            return
        name, emoji = parts[1].lower(), parts[2]
        await add_category(name, emoji)
        log.info("Category added: %s %s", emoji, name)
        await message.reply(f"✅ Category <b>{emoji} {name}</b> added.")

    @bot.on_message(filters.command("remove_category") & admin)
    async def cmd_remove_category(_, message: Message) -> None:
        parts = message.text.split()
        if len(parts) < 2:
            await message.reply("Usage: /remove_category <name>")
            return
        removed = await remove_category(parts[1].lower())
        if removed:
            log.info("Category removed: %s", parts[1])
        await message.reply("✅ Category removed." if removed else "Category not found.")

    @bot.on_message(filters.command("list_categories") & admin)
    async def cmd_list_categories(_, message: Message) -> None:
        cats = await get_categories()
        if not cats:
            await message.reply("No categories configured.")
            return
        lines = [f"{r['emoji']} <b>{r['name']}</b>" for r in cats]
        await message.reply("\n".join(lines))

    @bot.on_message(filters.command("digest") & admin)
    async def cmd_digest(_, message: Message) -> None:
        log.info("Manual digest triggered by user")
        await message.reply("⏳ Building digest...")
        await send_digest()
        await message.reply("✅ Digest sent.")

    @bot.on_message(filters.command("schedule") & admin)
    async def cmd_schedule(_, message: Message) -> None:
        parts = message.text.split()
        if len(parts) < 2:
            await message.reply("Usage: /schedule HH:MM")
            return

        time_str = parts[1]
        try:
            h, m = map(int, time_str.split(":"))
            assert 0 <= h < 24 and 0 <= m < 60
        except (ValueError, AssertionError):
            await message.reply("Invalid time format. Use HH:MM (e.g. 20:00)")
            return

        from src.scheduler import reschedule_digest
        reschedule_digest(time_str)
        log.info("Digest rescheduled to %s (%s)", time_str, settings.digest_timezone)
        await message.reply(f"✅ Digest scheduled at <b>{time_str}</b> ({settings.digest_timezone})")

    @bot.on_message(filters.command("stats") & admin)
    async def cmd_stats(_, message: Message) -> None:
        import aiosqlite
        async with aiosqlite.connect(settings.database_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT COUNT(*) as total, "
                "SUM(CASE WHEN importance='high' THEN 1 ELSE 0 END) as high, "
                "SUM(CASE WHEN importance='low' THEN 1 ELSE 0 END) as low "
                "FROM items WHERE processed_at >= datetime('now', '-24 hours')"
            ) as cur:
                row = await cur.fetchone()

        await message.reply(
            f"📊 <b>Last 24h</b>\n"
            f"Total processed: <b>{row['total']}</b>\n"
            f"High importance: <b>{row['high'] or 0}</b>\n"
            f"Low importance: <b>{row['low'] or 0}</b>"
        )

    @bot.on_message(filters.command("start") & admin)
    async def cmd_start(_, message: Message) -> None:
        await message.reply(
            "<b>TelegramSentinel</b>\n\n"
            "/add_source &lt;name&gt; &lt;url | @channel&gt; &lt;category&gt;\n"
            "/remove_source &lt;id&gt;\n"
            "/list_sources\n\n"
            "/add_category &lt;name&gt; &lt;emoji&gt;\n"
            "/remove_category &lt;name&gt;\n"
            "/list_categories\n\n"
            "/schedule &lt;HH:MM&gt;\n"
            "/digest — send now\n"
            "/stats"
        )

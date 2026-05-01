import asyncio
import logging

import feedparser
from pyrogram import filters
from pyrogram.types import Message

from src.collectors.folder_manager import add_to_folder, remove_from_folder
from src.collectors.telegram_collector import load_watched_channels, userbot
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

_pending: dict[int, dict] = {}


def _is_rss(url: str) -> bool:
    return url.startswith("http://") or url.startswith("https://")


def register_commands() -> None:
    admin = filters.user(settings.telegram_admin_id) & filters.private

    @bot.on_message(filters.command("cancel") & admin)
    async def cmd_cancel(_, message: Message) -> None:
        uid = message.from_user.id
        if uid in _pending:
            del _pending[uid]
            await message.reply("Cancelled.")
        else:
            await message.reply("Nothing to cancel.")

    @bot.on_message(filters.command("add_source") & admin)
    async def cmd_add_source(_, message: Message) -> None:
        _pending[message.from_user.id] = {"action": "add_source", "step": 0, "data": {}}
        await message.reply("URL or @channel:")

    @bot.on_message(filters.command("remove_source") & admin)
    async def cmd_remove_source(_, message: Message) -> None:
        sources = await get_active_sources()
        if not sources:
            await message.reply("No sources configured.")
            return
        lines = [
            f"<code>{r['id']}</code> [{r['type']}] <b>{r['name']}</b> [{r['category']}] — {r['url']}"
            for r in sources
        ]
        _pending[message.from_user.id] = {"action": "remove_source", "step": 0, "data": {}}
        await message.reply("\n".join(lines) + "\n\nEnter ID to remove:")

    @bot.on_message(filters.command("list_sources") & admin)
    async def cmd_list_sources(_, message: Message) -> None:
        sources = await get_active_sources()
        if not sources:
            await message.reply("No sources configured.")
            return
        lines = []
        for r in sources:
            status = ""
            if r["type"] == "telegram":
                username = r["url"].lstrip("@")
                try:
                    await userbot.get_chat(username)
                    status = " ✅"
                except Exception:
                    status = " ❌"
            lines.append(
                f"<code>{r['id']}</code> [{r['type']}] <b>{r['name']}</b> [{r['category']}] — {r['url']}{status}"
            )
        await message.reply("\n".join(lines))

    @bot.on_message(filters.command("add_category") & admin)
    async def cmd_add_category(_, message: Message) -> None:
        _pending[message.from_user.id] = {"action": "add_category", "step": 0, "data": {}}
        await message.reply("Category name:")

    @bot.on_message(filters.command("remove_category") & admin)
    async def cmd_remove_category(_, message: Message) -> None:
        cats = await get_categories()
        if not cats:
            await message.reply("No categories configured.")
            return
        lines = [f"{r['emoji']} <b>{r['name']}</b>" for r in cats]
        _pending[message.from_user.id] = {"action": "remove_category", "step": 0, "data": {}}
        await message.reply("\n".join(lines) + "\n\nCategory name to remove:")

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
        sent = await send_digest()
        await message.reply("✅ Digest sent." if sent else "ℹ️ No new items.")

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
            await message.reply("Invalid format. Use HH:MM (e.g. 20:00)")
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
            "/add_source — add RSS or Telegram channel\n"
            "/remove_source — remove source\n"
            "/list_sources\n\n"
            "/add_category — add category\n"
            "/remove_category — remove category\n"
            "/list_categories\n\n"
            "/schedule &lt;HH:MM&gt;\n"
            "/digest — send now\n"
            "/stats\n"
            "/cancel — cancel current input"
        )

    @bot.on_message(filters.private & admin)
    async def handle_conversation(_, message: Message) -> None:
        if not message.text or message.text.startswith("/"):
            return
        uid = message.from_user.id
        if uid not in _pending:
            return

        state = _pending[uid]
        text = message.text.strip()
        action = state["action"]
        step = state["step"]
        data = state["data"]

        if action == "add_category":
            if step == 0:
                data["name"] = text.lower()
                state["step"] = 1
                await message.reply("Emoji:")
            elif step == 1:
                data["emoji"] = text
                await add_category(data["name"], data["emoji"])
                del _pending[uid]
                log.info("Category added: %s %s", data["emoji"], data["name"])
                await message.reply(f"✅ Category <b>{data['emoji']} {data['name']}</b> added.")

        elif action == "add_source":
            if step == 0:
                url = text
                source_type = "rss" if _is_rss(url) else "telegram"
                if await source_exists(url):
                    del _pending[uid]
                    await message.reply(f"Source <code>{url}</code> already exists.")
                    return
                if source_type == "telegram":
                    try:
                        chat = await userbot.get_chat(url.lstrip("@"))
                        name = chat.title
                    except Exception as exc:
                        del _pending[uid]
                        await message.reply(f"Could not fetch channel info: {exc}")
                        return
                else:
                    feed = await asyncio.to_thread(feedparser.parse, url)
                    name = feed.feed.get("title") or url
                data["url"] = url
                data["name"] = name
                data["type"] = source_type
                state["step"] = 1
                cats = await get_categories()
                cats_hint = f" ({', '.join(r['name'] for r in cats)})" if cats else ""
                await message.reply(f"Name: <b>{name}</b>\nCategory{cats_hint}:")
            elif step == 1:
                data["category"] = text.lower()
                url = data["url"]
                source_type = data["type"]
                source_id = await add_source(source_type, data["name"], url, data["category"])
                if source_type == "telegram":
                    username = url.lstrip("@")
                    try:
                        await userbot.join_chat(username)
                        log.info("Userbot joined @%s", username)
                    except Exception as exc:
                        log.warning("Could not join @%s: %s", username, exc)
                    await add_to_folder(username)
                    await load_watched_channels()
                del _pending[uid]
                log.info("Source added: [%s] %s (%s) -> category=%s", source_type, data["name"], url, data["category"])
                await message.reply(
                    f"✅ Added [{source_type}] <b>{data['name']}</b> — <code>{url}</code>\n"
                    f"Category: <b>{data['category']}</b> | ID: <code>{source_id}</code>"
                )

        elif action == "remove_source":
            if not text.isdigit():
                await message.reply("Enter a numeric ID:")
                return
            sources = await get_active_sources()
            source = next((s for s in sources if s["id"] == int(text)), None)
            removed = await remove_source(int(text))
            del _pending[uid]
            if removed:
                if source and source["type"] == "telegram":
                    username = source["url"].lstrip("@")
                    await remove_from_folder(username)
                    try:
                        await userbot.leave_chat(username)
                        log.info("Userbot left @%s", username)
                    except Exception as exc:
                        log.warning("Could not leave @%s: %s", username, exc)
                await load_watched_channels()
                log.info("Source removed: id=%s", text)
                await message.reply("✅ Source removed.")
            else:
                await message.reply("Source not found.")

        elif action == "remove_category":
            removed = await remove_category(text.lower())
            del _pending[uid]
            if removed:
                log.info("Category removed: %s", text)
            await message.reply("✅ Category removed." if removed else "Category not found.")

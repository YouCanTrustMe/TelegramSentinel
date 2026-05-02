import asyncio
import logging
from html import escape

import feedparser
from pyrogram import filters
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    ReplyKeyboardRemove,
)

from src.collectors.folder_manager import add_to_folder, remove_from_folder
from src.collectors.telegram_collector import load_watched_channels, userbot
from src.config import settings
from src.db.models import (
    add_category,
    add_source,
    category_exists,
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


def _cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_flow")]])


def _back_kb(back_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀ Back", callback_data=back_data)]])


def _categories_keyboard(cats) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(f"{r['emoji']} {r['name']}", callback_data=f"cat_view:{r['name']}")] for r in cats]
    )


def _category_view_keyboard(cat_name: str, sources) -> InlineKeyboardMarkup:
    buttons = []
    for s in sources:
        icon = "📡" if s["type"] == "telegram" else "🔗"
        type_label = "tg" if s["type"] == "telegram" else "rss"
        buttons.append([InlineKeyboardButton(f"{icon} [{type_label}] {s['name']}", callback_data=f"src_view:{s['id']}")])
    buttons.append([
        InlineKeyboardButton("➕ Add source", callback_data=f"src_add:{cat_name}"),
        InlineKeyboardButton("🗑 Delete", callback_data=f"cat_del:{cat_name}"),
    ])
    buttons.append([InlineKeyboardButton("◀ Back", callback_data="cat_list")])
    return InlineKeyboardMarkup(buttons)


def _source_view_keyboard(source_id: int, cat_name: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑 Remove source", callback_data=f"src_del:{source_id}")],
        [InlineKeyboardButton("◀ Back", callback_data=f"cat_view:{cat_name}")],
    ])


def _confirm_keyboard(yes_data: str, no_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes", callback_data=yes_data),
        InlineKeyboardButton("❌ No", callback_data=no_data),
    ]])


def register_commands() -> None:
    admin_msg = filters.user(settings.telegram_admin_id) & filters.private
    admin_cb = filters.user(settings.telegram_admin_id)

    # ─── Misc ─────────────────────────────────────────────────────────────

    @bot.on_message(filters.command("cancel") & admin_msg)
    async def cmd_cancel(_, message: Message) -> None:
        uid = message.from_user.id
        if uid in _pending:
            del _pending[uid]
            await message.reply("Cancelled.")
        else:
            await message.reply("Nothing to cancel.")

    @bot.on_callback_query(filters.regex(r"^cancel_flow$") & admin_cb)
    async def cb_cancel_flow(_, query: CallbackQuery) -> None:
        _pending.pop(query.from_user.id, None)
        await query.message.edit_text("Cancelled.")

    @bot.on_callback_query(filters.regex(r"^noop$") & admin_cb)
    async def cb_noop(_, query: CallbackQuery) -> None:
        await query.answer()

    # ─── Categories ───────────────────────────────────────────────────────

    @bot.on_message(filters.command("list_categories") & admin_msg)
    async def cmd_list_categories(_, message: Message) -> None:
        cats = await get_categories()
        if not cats:
            await message.reply("No categories configured.")
            return
        await message.reply("Categories:", reply_markup=_categories_keyboard(cats))

    @bot.on_callback_query(filters.regex(r"^cat_list$") & admin_cb)
    async def cb_cat_list(_, query: CallbackQuery) -> None:
        _pending.pop(query.from_user.id, None)
        cats = await get_categories()
        if not cats:
            await query.message.edit_text("No categories configured.")
            return
        await query.message.edit_text("Categories:", reply_markup=_categories_keyboard(cats))

    @bot.on_callback_query(filters.regex(r"^cat_view:") & admin_cb)
    async def cb_cat_view(_, query: CallbackQuery) -> None:
        _pending.pop(query.from_user.id, None)
        cat_name = query.data.split(":", 1)[1]
        cats = await get_categories()
        sources = [s for s in await get_active_sources() if s["category"] == cat_name]
        cat = next((c for c in cats if c["name"] == cat_name), None)
        emoji = cat["emoji"] if cat else "📌"

        if not sources:
            text = f"<b>{emoji} {cat_name}</b>\n\nNo sources yet."
        else:
            lines = [f"<b>{emoji} {cat_name}</b>\n"]
            for s in sources:
                icon = "📡" if s["type"] == "telegram" else "🔗"
                type_label = "tg" if s["type"] == "telegram" else "rss"
                lines.append(f"{icon} [{type_label}] <b>{s['name']}</b> — <code>{s['url']}</code>")
            text = "\n".join(lines)

        await query.message.edit_text(text, reply_markup=_category_view_keyboard(cat_name, sources))

    @bot.on_callback_query(filters.regex(r"^cat_del:") & admin_cb)
    async def cb_cat_del(_, query: CallbackQuery) -> None:
        cat_name = query.data.split(":", 1)[1]
        await query.message.edit_text(
            f"Delete category <b>{cat_name}</b>?",
            reply_markup=_confirm_keyboard(f"cat_del_ok:{cat_name}", f"cat_view:{cat_name}"),
        )

    @bot.on_callback_query(filters.regex(r"^cat_del_ok:") & admin_cb)
    async def cb_cat_del_ok(_, query: CallbackQuery) -> None:
        cat_name = query.data.split(":", 1)[1]
        removed = await remove_category(cat_name)
        if removed:
            log.info("Category removed: %s", cat_name)
        cats = await get_categories()
        if not cats:
            await query.message.edit_text("✅ Category removed. No categories left.")
            return
        await query.message.edit_text("✅ Category removed.\n\nCategories:", reply_markup=_categories_keyboard(cats))

    @bot.on_message(filters.command("add_category") & admin_msg)
    async def cmd_add_category(_, message: Message) -> None:
        _pending[message.from_user.id] = {"action": "add_category", "step": 0, "data": {}}
        await message.reply("Category name:", reply_markup=_cancel_kb())

    # ─── Sources ──────────────────────────────────────────────────────────

    @bot.on_message((filters.command("list_sources") | filters.command("remove_source")) & admin_msg)
    async def cmd_list_sources(_, message: Message) -> None:
        sources = await get_active_sources()
        if not sources:
            await message.reply("No sources configured.")
            return
        cats = await get_categories()
        cat_meta = {c["name"]: c for c in cats}
        by_cat: dict[str, list] = {}
        for s in sources:
            by_cat.setdefault(s["category"], []).append(s)

        buttons = []
        for cat_name in list(cat_meta) + [k for k in by_cat if k not in cat_meta]:
            if cat_name not in by_cat:
                continue
            c = cat_meta.get(cat_name)
            emoji = c["emoji"] if c else "📌"
            buttons.append([InlineKeyboardButton(f"── {emoji} {cat_name} ──", callback_data="noop")])
            for s in by_cat[cat_name]:
                icon = "📡" if s["type"] == "telegram" else "🔗"
                type_label = "tg" if s["type"] == "telegram" else "rss"
                buttons.append([InlineKeyboardButton(f"{icon} [{type_label}] {s['name']}", callback_data=f"src_view:{s['id']}")])

        await message.reply("Sources:", reply_markup=InlineKeyboardMarkup(buttons))

    @bot.on_callback_query(filters.regex(r"^src_view:") & admin_cb)
    async def cb_src_view(_, query: CallbackQuery) -> None:
        src_id = int(query.data.split(":", 1)[1])
        sources = await get_active_sources()
        s = next((x for x in sources if x["id"] == src_id), None)
        if not s:
            await query.answer("Source not found.", show_alert=True)
            return
        icon = "📡" if s["type"] == "telegram" else "🔗"
        type_label = "tg" if s["type"] == "telegram" else "rss"
        text = (
            f"{icon} <b>{s['name']}</b>\n"
            f"Type: <code>{type_label}</code>\n"
            f"URL: <code>{s['url']}</code>\n"
            f"Category: <b>{s['category']}</b>"
        )
        await query.message.edit_text(text, reply_markup=_source_view_keyboard(src_id, s["category"]))

    @bot.on_callback_query(filters.regex(r"^src_del:") & admin_cb)
    async def cb_src_del(_, query: CallbackQuery) -> None:
        src_id = int(query.data.split(":", 1)[1])
        sources = await get_active_sources()
        s = next((x for x in sources if x["id"] == src_id), None)
        name = s["name"] if s else str(src_id)
        cat = s["category"] if s else ""
        await query.message.edit_text(
            f"Remove source <b>{name}</b>?",
            reply_markup=_confirm_keyboard(f"src_del_ok:{src_id}", f"cat_view:{cat}"),
        )

    @bot.on_callback_query(filters.regex(r"^src_del_ok:") & admin_cb)
    async def cb_src_del_ok(_, query: CallbackQuery) -> None:
        src_id = int(query.data.split(":", 1)[1])
        sources = await get_active_sources()
        s = next((x for x in sources if x["id"] == src_id), None)
        cat_name = s["category"] if s else None

        if s and s["type"] == "telegram":
            username = s["url"].lstrip("@")
            await remove_from_folder(username)
            try:
                await userbot.leave_chat(username)
                log.info("Userbot left @%s", username)
            except Exception as exc:
                log.warning("Could not leave @%s: %s", username, exc)

        removed = await remove_source(src_id)
        await load_watched_channels()
        if removed:
            log.info("Source removed: id=%s", src_id)

        if cat_name:
            cats = await get_categories()
            remaining = [x for x in await get_active_sources() if x["category"] == cat_name]
            cat = next((c for c in cats if c["name"] == cat_name), None)
            emoji = cat["emoji"] if cat else "📌"
            if not remaining:
                text = f"✅ Source removed.\n\n<b>{emoji} {cat_name}</b>\n\nNo sources left."
            else:
                lines = [f"✅ Source removed.\n\n<b>{emoji} {cat_name}</b>\n"]
                for x in remaining:
                    icon = "📡" if x["type"] == "telegram" else "🔗"
                    type_label = "tg" if x["type"] == "telegram" else "rss"
                    lines.append(f"{icon} [{type_label}] <b>{x['name']}</b> — <code>{x['url']}</code>")
                text = "\n".join(lines)
            await query.message.edit_text(text, reply_markup=_category_view_keyboard(cat_name, remaining))
        else:
            await query.message.edit_text("✅ Source removed.")

    @bot.on_callback_query(filters.regex(r"^src_add:") & admin_cb)
    async def cb_src_add(_, query: CallbackQuery) -> None:
        cat_name = query.data.split(":", 1)[1]
        uid = query.from_user.id
        _pending[uid] = {"action": "add_source", "step": 0, "data": {"preset_category": cat_name}}
        await query.message.edit_text(
            f"Adding source to <b>{cat_name}</b>.\n\nURL or @channel:",
            reply_markup=_back_kb(f"cat_view:{cat_name}"),
        )

    @bot.on_message(filters.command("add_source") & admin_msg)
    async def cmd_add_source(_, message: Message) -> None:
        _pending[message.from_user.id] = {"action": "add_source", "step": 0, "data": {}}
        await message.reply("URL or @channel:", reply_markup=_cancel_kb())

    # ─── Digest & Schedule ────────────────────────────────────────────────

    @bot.on_message(filters.command("digest") & admin_msg)
    async def cmd_digest(_, message: Message) -> None:
        log.info("Manual digest triggered by user")
        await message.reply("⏳ Building digest...")
        sent = await send_digest()
        await message.reply("✅ Digest sent." if sent else "ℹ️ No new items.")

    @bot.on_message(filters.command("schedule") & admin_msg)
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
        try:
            reschedule_digest(time_str)
        except Exception as exc:
            log.error("Failed to reschedule digest: %s", exc)
            await message.reply(f"❌ Failed to reschedule: {exc}")
            return
        log.info("Digest rescheduled to %s (%s)", time_str, settings.digest_timezone)
        await message.reply(f"✅ Digest scheduled at <b>{time_str}</b> ({settings.digest_timezone})")

    @bot.on_message(filters.command("stats") & admin_msg)
    async def cmd_stats(_, message: Message) -> None:
        import aiosqlite
        async with aiosqlite.connect(settings.database_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT COUNT(*) as total FROM items WHERE processed_at >= datetime('now', '-24 hours')"
            ) as cur:
                total = (await cur.fetchone())["total"]
            async with db.execute("SELECT COUNT(*) as unsent FROM items WHERE sent = 0") as cur:
                unsent = (await cur.fetchone())["unsent"]
            async with db.execute(
                """SELECT sources.name,
                          COUNT(*) as cnt,
                          SUM(CASE WHEN items.sent = 0 THEN 1 ELSE 0 END) as unsent_cnt
                   FROM items JOIN sources ON items.source_id = sources.id
                   WHERE items.processed_at >= datetime('now', '-24 hours')
                   GROUP BY sources.id ORDER BY cnt DESC"""
            ) as cur:
                by_source = await cur.fetchall()

        lines = [f"📊 <b>Stats</b>", f"Processed 24h: <b>{total}</b> · Unsent: <b>{unsent}</b>"]
        if by_source:
            lines.append("")
            for r in by_source:
                lines.append(f"• {r['name']}: <b>{r['cnt']}</b> · unsent <b>{r['unsent_cnt']}</b>")
        await message.reply("\n".join(lines))

    @bot.on_message(filters.command("start") & admin_msg)
    async def cmd_start(_, message: Message) -> None:
        await message.reply(
            "<b>TelegramSentinel</b>\n\n"
            "/list_categories — browse &amp; manage categories\n"
            "/add_category — add category\n\n"
            "/list_sources — view &amp; manage sources\n"
            "/add_source — add RSS or Telegram channel\n\n"
            "/schedule &lt;HH:MM&gt; — set digest time\n"
            "/digest — send now\n"
            "/stats\n"
            "/cancel — cancel current input"
        )

    # ─── Conversation handler ─────────────────────────────────────────────

    @bot.on_callback_query(filters.regex(r"^add_src_cat:") & admin_cb)
    async def cb_add_src_cat(_, query: CallbackQuery) -> None:
        cat = query.data.split(":", 1)[1]
        uid = query.from_user.id
        if uid not in _pending or _pending[uid].get("action") != "add_source":
            await query.answer("Session expired. Use /add_source again.", show_alert=True)
            return
        data = _pending[uid]["data"]
        await query.message.edit_text(f"Name: <b>{data['name']}</b>\nCategory: <b>{cat}</b>")
        await _finalize_add_source(uid, cat, data, query.message, reply=False)

    async def _finalize_add_source(uid: int, cat: str, data: dict, message, reply: bool = True) -> None:
        source_type = data["type"]
        url = data["url"]
        name = data["name"]
        cat = cat.lower()
        if not await category_exists(cat):
            await add_category(cat, "📌")
            log.info("Auto-created category: 📌 %s", cat)
        source_id = await add_source(source_type, name, url, cat)
        join_warning = ""
        if source_type == "telegram":
            username = url.lstrip("@")
            try:
                await userbot.join_chat(username)
                log.info("Userbot joined @%s", username)
            except Exception as exc:
                log.warning("Could not join @%s: %s", username, exc)
                join_warning = f"\n⚠️ Userbot failed to join: {exc}"
            await add_to_folder(username)
            await load_watched_channels()
        del _pending[uid]
        log.info("Source added: [%s] %s (%s) -> category=%s", source_type, name, url, cat)
        text = (
            f"✅ Added [{source_type}] <b>{name}</b> — <code>{url}</code>\n"
            f"Category: <b>{cat}</b>{join_warning}"
        )
        if reply:
            await message.reply(text, reply_markup=ReplyKeyboardRemove())
        else:
            await message.reply(text)

    @bot.on_message(filters.private & admin_msg)
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
                await message.reply("Emoji:", reply_markup=_cancel_kb())
            elif step == 1:
                data["emoji"] = escape(text[:8])
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

                if "preset_category" in data:
                    await _finalize_add_source(uid, data["preset_category"], data, message)
                    return

                state["step"] = 1
                cats = await get_categories()
                if cats:
                    keyboard = InlineKeyboardMarkup(
                        [[InlineKeyboardButton(f"{r['emoji']} {r['name']}", callback_data=f"add_src_cat:{r['name']}")] for r in cats]
                        + [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_flow")]]
                    )
                    await message.reply(f"Name: <b>{name}</b>\nCategory:", reply_markup=keyboard)
                else:
                    await message.reply(f"Name: <b>{name}</b>\nCategory:", reply_markup=_cancel_kb())

            elif step == 1:
                await _finalize_add_source(uid, text, data, message)

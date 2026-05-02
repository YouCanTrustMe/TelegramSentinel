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
    delete_sources_by_category,
    get_active_sources,
    get_categories,
    get_sources_by_category,
    move_sources_to_category,
    remove_category,
    remove_source,
    source_exists,
    update_category,
)
from src.dispatcher.digest_builder import send_digest
from src.dispatcher.sender import bot
from src.scheduler import rebuild_digest_jobs

log = logging.getLogger(__name__)

_pending: dict[int, dict] = {}

_DEFAULT_DIGEST_TIME = "21:00"


def _is_rss(url: str) -> bool:
    return url.startswith("http://") or url.startswith("https://")


def _is_valid_time(t: str) -> bool:
    try:
        h, m = map(int, t.split(":"))
        return 0 <= h < 24 and 0 <= m < 60
    except (ValueError, AttributeError):
        return False


def _cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_flow")]])


def _back_kb(back_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀ Back", callback_data=back_data)]])


def _categories_keyboard(cats) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(f"{r['emoji']} {r['name']} · {r['digest_time']}", callback_data=f"cat_view:{r['name']}")]
        for r in cats
    ]
    buttons.append([InlineKeyboardButton("➕ Add category", callback_data="cat_add")])
    return InlineKeyboardMarkup(buttons)


def _category_view_keyboard(cat_name: str, sources) -> InlineKeyboardMarkup:
    buttons = []
    for s in sources:
        icon = "📡" if s["type"] == "telegram" else "🔗"
        type_label = "tg" if s["type"] == "telegram" else "rss"
        buttons.append([InlineKeyboardButton(f"{icon} [{type_label}] {s['name']}", callback_data=f"src_view:{s['id']}")])
    buttons.append([
        InlineKeyboardButton("➕ Add source", callback_data=f"src_add:{cat_name}"),
        InlineKeyboardButton("✏️ Edit", callback_data=f"cat_edit:{cat_name}"),
        InlineKeyboardButton("🗑 Delete", callback_data=f"cat_del:{cat_name}"),
    ])
    buttons.append([InlineKeyboardButton("◀ Back", callback_data="cat_list")])
    return InlineKeyboardMarkup(buttons)


def _cat_edit_keyboard(cat_name: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Rename", callback_data=f"cat_edit_field:{cat_name}:name")],
        [InlineKeyboardButton("🎨 Change emoji", callback_data=f"cat_edit_field:{cat_name}:emoji")],
        [InlineKeyboardButton("🕐 Change digest time", callback_data=f"cat_edit_field:{cat_name}:time")],
        [InlineKeyboardButton("◀ Back", callback_data=f"cat_view:{cat_name}")],
    ])


def _source_view_keyboard(source_id: int, cat_name: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Reassign category", callback_data=f"src_reassign:{source_id}")],
        [InlineKeyboardButton("🗑 Remove source", callback_data=f"src_del:{source_id}")],
        [InlineKeyboardButton("◀ Back", callback_data=f"cat_view:{cat_name}")],
    ])


def _confirm_keyboard(yes_data: str, no_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes", callback_data=yes_data),
        InlineKeyboardButton("❌ No", callback_data=no_data),
    ]])


def _time_step_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"⏭ Default ({_DEFAULT_DIGEST_TIME})", callback_data="cat_add_time_default")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_flow")],
    ])


def _edit_time_kb(back_cat: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel", callback_data=f"cat_edit:{back_cat}")],
    ])


async def _cat_view_text(cat_name: str) -> tuple[str, list]:
    cats = await get_categories()
    sources = [s for s in await get_active_sources() if s["category"] == cat_name]
    cat = next((c for c in cats if c["name"] == cat_name), None)
    emoji = cat["emoji"] if cat else "📌"
    digest_time = cat["digest_time"] if cat else _DEFAULT_DIGEST_TIME

    if not sources:
        text = f"<b>{emoji} {cat_name}</b>  ·  ⏰ {digest_time}\n\nNo sources yet."
    else:
        lines = [f"<b>{emoji} {cat_name}</b>  ·  ⏰ {digest_time}\n"]
        for s in sources:
            icon = "📡" if s["type"] == "telegram" else "🔗"
            type_label = "tg" if s["type"] == "telegram" else "rss"
            lines.append(f"{icon} [{type_label}] <b>{s['name']}</b> — <code>{s['url']}</code>")
        text = "\n".join(lines)
    return text, sources


def register_commands() -> None:
    admin_msg = filters.user(settings.telegram_admin_id) & filters.private
    admin_cb = filters.user(settings.telegram_admin_id)

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

    @bot.on_message(filters.command("categories") & admin_msg)
    async def cmd_categories(_, message: Message) -> None:
        cats = await get_categories()
        if not cats:
            await message.reply("No categories yet.", reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("➕ Add category", callback_data="cat_add")]]
            ))
            return
        await message.reply("Categories:", reply_markup=_categories_keyboard(cats))

    @bot.on_callback_query(filters.regex(r"^cat_list$") & admin_cb)
    async def cb_cat_list(_, query: CallbackQuery) -> None:
        _pending.pop(query.from_user.id, None)
        cats = await get_categories()
        if not cats:
            await query.message.edit_text("No categories yet.", reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("➕ Add category", callback_data="cat_add")]]
            ))
            return
        await query.message.edit_text("Categories:", reply_markup=_categories_keyboard(cats))

    @bot.on_callback_query(filters.regex(r"^cat_view:") & admin_cb)
    async def cb_cat_view(_, query: CallbackQuery) -> None:
        _pending.pop(query.from_user.id, None)
        cat_name = query.data.split(":", 1)[1]
        text, sources = await _cat_view_text(cat_name)
        await query.message.edit_text(text, reply_markup=_category_view_keyboard(cat_name, sources))

    @bot.on_callback_query(filters.regex(r"^cat_add$") & admin_cb)
    async def cb_cat_add(_, query: CallbackQuery) -> None:
        uid = query.from_user.id
        _pending[uid] = {"action": "add_category", "step": 0, "data": {}}
        await query.message.edit_text("Category name:", reply_markup=_cancel_kb())

    @bot.on_callback_query(filters.regex(r"^cat_add_time_default$") & admin_cb)
    async def cb_cat_add_time_default(_, query: CallbackQuery) -> None:
        uid = query.from_user.id
        if uid not in _pending or _pending[uid].get("action") != "add_category":
            await query.answer("Session expired.", show_alert=True)
            return
        data = _pending[uid]["data"]
        data["digest_time"] = _DEFAULT_DIGEST_TIME
        await _finalize_add_category(uid, data, query.message, reply=False)

    @bot.on_callback_query(filters.regex(r"^cat_del:") & admin_cb)
    async def cb_cat_del(_, query: CallbackQuery) -> None:
        cat_name = query.data.split(":", 1)[1]
        sources = await get_sources_by_category(cat_name)
        if not sources:
            await query.message.edit_text(
                f"Delete category <b>{cat_name}</b>?",
                reply_markup=_confirm_keyboard(f"cat_del_ok:{cat_name}", f"cat_view:{cat_name}"),
            )
            return

        cats = await get_categories()
        other_cats = [c for c in cats if c["name"] != cat_name]
        src_count = len(sources)
        buttons = []
        for c in other_cats:
            buttons.append([InlineKeyboardButton(
                f"Move to {c['emoji']} {c['name']}",
                callback_data=f"cat_del_move:{cat_name}:{c['name']}",
            )])
        buttons.append([InlineKeyboardButton(f"🗑 Delete all {src_count} source(s) too", callback_data=f"cat_del_all:{cat_name}")])
        buttons.append([InlineKeyboardButton("❌ Cancel", callback_data=f"cat_view:{cat_name}")])
        await query.message.edit_text(
            f"<b>{cat_name}</b> has <b>{src_count}</b> source(s). What to do with them?",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    @bot.on_callback_query(filters.regex(r"^cat_del_ok:") & admin_cb)
    async def cb_cat_del_ok(_, query: CallbackQuery) -> None:
        cat_name = query.data.split(":", 1)[1]
        removed = await remove_category(cat_name)
        if removed:
            log.info("Category removed: %s", cat_name)
            await rebuild_digest_jobs()
        cats = await get_categories()
        if not cats:
            await query.message.edit_text("✅ Category removed. No categories left.", reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("➕ Add category", callback_data="cat_add")]]
            ))
            return
        await query.message.edit_text("✅ Category removed.\n\nCategories:", reply_markup=_categories_keyboard(cats))

    @bot.on_callback_query(filters.regex(r"^cat_del_move:") & admin_cb)
    async def cb_cat_del_move(_, query: CallbackQuery) -> None:
        _, from_cat, to_cat = query.data.split(":", 2)
        await move_sources_to_category(from_cat, to_cat)
        await remove_category(from_cat)
        await rebuild_digest_jobs()
        log.info("Category %s deleted, sources moved to %s", from_cat, to_cat)
        cats = await get_categories()
        text = f"✅ Sources moved to <b>{to_cat}</b>, category <b>{from_cat}</b> deleted.\n\nCategories:"
        if not cats:
            await query.message.edit_text(text.replace("\n\nCategories:", ""))
        else:
            await query.message.edit_text(text, reply_markup=_categories_keyboard(cats))

    @bot.on_callback_query(filters.regex(r"^cat_del_all:") & admin_cb)
    async def cb_cat_del_all(_, query: CallbackQuery) -> None:
        cat_name = query.data.split(":", 1)[1]
        await delete_sources_by_category(cat_name)
        await remove_category(cat_name)
        await rebuild_digest_jobs()
        log.info("Category %s deleted with all sources", cat_name)
        cats = await get_categories()
        if not cats:
            await query.message.edit_text("✅ Category and all sources deleted.", reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("➕ Add category", callback_data="cat_add")]]
            ))
            return
        await query.message.edit_text("✅ Category and all sources deleted.\n\nCategories:", reply_markup=_categories_keyboard(cats))

    @bot.on_callback_query(filters.regex(r"^cat_edit:") & admin_cb)
    async def cb_cat_edit(_, query: CallbackQuery) -> None:
        _pending.pop(query.from_user.id, None)
        cat_name = query.data.split(":", 1)[1]
        cats = await get_categories()
        cat = next((c for c in cats if c["name"] == cat_name), None)
        emoji = cat["emoji"] if cat else "📌"
        digest_time = cat["digest_time"] if cat else _DEFAULT_DIGEST_TIME
        await query.message.edit_text(
            f"✏️ Edit <b>{emoji} {cat_name}</b>  ·  ⏰ {digest_time}",
            reply_markup=_cat_edit_keyboard(cat_name),
        )

    @bot.on_callback_query(filters.regex(r"^cat_edit_field:") & admin_cb)
    async def cb_cat_edit_field(_, query: CallbackQuery) -> None:
        parts = query.data.split(":", 2)
        cat_name, field = parts[1], parts[2]
        uid = query.from_user.id
        _pending[uid] = {"action": "edit_category", "step": 0, "data": {"cat_name": cat_name, "field": field}}

        if field == "name":
            await query.message.edit_text(
                f"New name for <b>{cat_name}</b>:",
                reply_markup=_back_kb(f"cat_edit:{cat_name}"),
            )
        elif field == "emoji":
            await query.message.edit_text(
                f"New emoji for <b>{cat_name}</b>:",
                reply_markup=_back_kb(f"cat_edit:{cat_name}"),
            )
        elif field == "time":
            cats = await get_categories()
            cat = next((c for c in cats if c["name"] == cat_name), None)
            current = cat["digest_time"] if cat else _DEFAULT_DIGEST_TIME
            await query.message.edit_text(
                f"New digest time for <b>{cat_name}</b> (HH:MM):\nCurrent: <b>{current}</b>",
                reply_markup=_edit_time_kb(cat_name),
            )

    @bot.on_message(filters.command("sources") & admin_msg)
    async def cmd_sources(_, message: Message) -> None:
        await _send_sources_list(message, reply=True)

    @bot.on_callback_query(filters.regex(r"^src_list$") & admin_cb)
    async def cb_src_list(_, query: CallbackQuery) -> None:
        _pending.pop(query.from_user.id, None)
        await _send_sources_list(query.message, reply=False)

    async def _send_sources_list(target, reply: bool) -> None:
        sources = await get_active_sources()
        if not sources:
            text = "No sources configured."
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("➕ Add source", callback_data="src_add:")]])
            if reply:
                await target.reply(text, reply_markup=kb)
            else:
                await target.edit_text(text, reply_markup=kb)
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

        buttons.append([InlineKeyboardButton("➕ Add source", callback_data="src_add:")])
        kb = InlineKeyboardMarkup(buttons)
        if reply:
            await target.reply("Sources:", reply_markup=kb)
        else:
            await target.edit_text("Sources:", reply_markup=kb)

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

    @bot.on_callback_query(filters.regex(r"^src_reassign:") & admin_cb)
    async def cb_src_reassign(_, query: CallbackQuery) -> None:
        src_id = int(query.data.split(":", 1)[1])
        cats = await get_categories()
        if not cats:
            await query.answer("No categories available.", show_alert=True)
            return
        buttons = [
            [InlineKeyboardButton(f"{c['emoji']} {c['name']}", callback_data=f"src_reassign_to:{src_id}:{c['name']}")]
            for c in cats
        ]
        buttons.append([InlineKeyboardButton("❌ Cancel", callback_data=f"src_view:{src_id}")])
        await query.message.edit_text("Move to category:", reply_markup=InlineKeyboardMarkup(buttons))

    @bot.on_callback_query(filters.regex(r"^src_reassign_to:") & admin_cb)
    async def cb_src_reassign_to(_, query: CallbackQuery) -> None:
        _, src_id_str, cat_name = query.data.split(":", 2)
        src_id = int(src_id_str)
        async with __import__("aiosqlite").connect(settings.database_path) as db:
            await db.execute("UPDATE sources SET category = ? WHERE id = ?", (cat_name, src_id))
            await db.execute("UPDATE items SET category = ? WHERE source_id = ? AND sent = 0", (cat_name, src_id))
            await db.commit()
        log.info("Source id=%s reassigned to category=%s", src_id, cat_name)
        sources = await get_active_sources()
        s = next((x for x in sources if x["id"] == src_id), None)
        if s:
            icon = "📡" if s["type"] == "telegram" else "🔗"
            type_label = "tg" if s["type"] == "telegram" else "rss"
            text = (
                f"{icon} <b>{s['name']}</b>\n"
                f"Type: <code>{type_label}</code>\n"
                f"URL: <code>{s['url']}</code>\n"
                f"Category: <b>{s['category']}</b>"
            )
            await query.message.edit_text(f"✅ Reassigned.\n\n{text}", reply_markup=_source_view_keyboard(src_id, cat_name))
        else:
            await query.message.edit_text("✅ Reassigned.")

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
            text, remaining = await _cat_view_text(cat_name)
            prefix = "✅ Source removed.\n\n"
            await query.message.edit_text(
                prefix + text,
                reply_markup=_category_view_keyboard(cat_name, remaining),
            )
        else:
            await query.message.edit_text("✅ Source removed.")

    @bot.on_callback_query(filters.regex(r"^src_add:") & admin_cb)
    async def cb_src_add(_, query: CallbackQuery) -> None:
        cat_name = query.data.split(":", 1)[1]  # may be empty string
        uid = query.from_user.id
        data: dict = {}
        if cat_name:
            data["preset_category"] = cat_name
        _pending[uid] = {"action": "add_source", "step": 0, "data": data}
        back = f"cat_view:{cat_name}" if cat_name else "src_list"
        prompt = f"Adding source to <b>{cat_name}</b>.\n\nURL or @channel:" if cat_name else "URL or @channel:"
        await query.message.edit_text(prompt, reply_markup=_back_kb(back))

    @bot.on_message(filters.command("digest") & admin_msg)
    async def cmd_digest(_, message: Message) -> None:
        log.info("Manual digest triggered by user")
        status_msg = await message.reply("⏳ Building digest...")
        sent = await send_digest()
        await status_msg.delete()
        await message.reply("✅ Digest sent." if sent else "ℹ️ No new items.")

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

        lines = ["📊 <b>Stats</b>", f"Processed 24h: <b>{total}</b> · Unsent: <b>{unsent}</b>"]
        if by_source:
            lines.append("")
            for r in by_source:
                lines.append(f"• {r['name']}: <b>{r['cnt']}</b> · unsent <b>{r['unsent_cnt']}</b>")
        await message.reply("\n".join(lines))

    @bot.on_message(filters.command("start") & admin_msg)
    async def cmd_start(_, message: Message) -> None:
        await message.reply(
            "<b>TelegramSentinel</b>\n\n"
            "/categories — manage categories &amp; sources\n"
            "/sources — all sources overview\n\n"
            "/digest — send digest now\n"
            "/stats — statistics\n"
            "/cancel — cancel current input"
        )

    @bot.on_callback_query(filters.regex(r"^add_src_cat:") & admin_cb)
    async def cb_add_src_cat(_, query: CallbackQuery) -> None:
        cat = query.data.split(":", 1)[1]
        uid = query.from_user.id
        if uid not in _pending or _pending[uid].get("action") != "add_source":
            await query.answer("Session expired. Use /sources again.", show_alert=True)
            return
        data = _pending[uid]["data"]
        await query.message.edit_text(f"Name: <b>{data['name']}</b>\nCategory: <b>{cat}</b>")
        await _finalize_add_source(uid, cat, data, query.message, reply=False)

    async def _finalize_add_category(uid: int, data: dict, message, reply: bool = True) -> None:
        name = data["name"]
        emoji = data["emoji"]
        digest_time = data.get("digest_time", _DEFAULT_DIGEST_TIME)
        await add_category(name, emoji, digest_time)
        del _pending[uid]
        await rebuild_digest_jobs()
        log.info("Category added: %s %s digest_time=%s", emoji, name, digest_time)
        text = f"✅ Category <b>{emoji} {name}</b> added.  ⏰ {digest_time}"
        if reply:
            await message.reply(text)
        else:
            await message.edit_text(text)

    async def _finalize_add_source(uid: int, cat: str, data: dict, message, reply: bool = True) -> None:
        source_type = data["type"]
        url = data["url"]
        name = data["name"]
        cat = cat.lower()
        if not await category_exists(cat):
            await add_category(cat, "📌")
            log.info("Auto-created category: 📌 %s", cat)
        await add_source(source_type, name, url, cat)
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
                state["step"] = 2
                await message.reply(
                    f"Digest time (HH:MM) or skip for default ({_DEFAULT_DIGEST_TIME}):",
                    reply_markup=_time_step_kb(),
                )
            elif step == 2:
                if not _is_valid_time(text):
                    await message.reply(
                        "Invalid format. Use HH:MM (e.g. 16:00):",
                        reply_markup=_time_step_kb(),
                    )
                    return
                data["digest_time"] = text
                await _finalize_add_category(uid, data, message)

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

        elif action == "edit_category":
            field = data["field"]
            cat_name = data["cat_name"]

            if field == "name":
                new_name = text.lower()
                ok = await update_category(cat_name, new_name=new_name)
                del _pending[uid]
                if ok:
                    await rebuild_digest_jobs()
                    log.info("Category renamed: %s -> %s", cat_name, new_name)
                    text_out, sources = await _cat_view_text(new_name)
                    await message.reply(
                        f"✅ Renamed to <b>{new_name}</b>.\n\n" + text_out,
                        reply_markup=_category_view_keyboard(new_name, sources),
                    )
                else:
                    await message.reply("Category not found.")

            elif field == "emoji":
                new_emoji = escape(text[:8])
                ok = await update_category(cat_name, new_emoji=new_emoji)
                del _pending[uid]
                if ok:
                    log.info("Category emoji updated: %s -> %s", cat_name, new_emoji)
                    text_out, sources = await _cat_view_text(cat_name)
                    await message.reply(
                        f"✅ Emoji updated.\n\n" + text_out,
                        reply_markup=_category_view_keyboard(cat_name, sources),
                    )
                else:
                    await message.reply("Category not found.")

            elif field == "time":
                if not _is_valid_time(text):
                    await message.reply(
                        "Invalid format. Use HH:MM (e.g. 16:00):",
                        reply_markup=_edit_time_kb(cat_name),
                    )
                    return
                ok = await update_category(cat_name, new_digest_time=text)
                del _pending[uid]
                if ok:
                    await rebuild_digest_jobs()
                    log.info("Category digest_time updated: %s -> %s", cat_name, text)
                    text_out, sources = await _cat_view_text(cat_name)
                    await message.reply(
                        f"✅ Digest time set to <b>{text}</b>.\n\n" + text_out,
                        reply_markup=_category_view_keyboard(cat_name, sources),
                    )
                else:
                    await message.reply("Category not found.")

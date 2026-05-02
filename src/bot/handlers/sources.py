import logging

import aiosqlite
from pyrogram import filters as pf
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message, ReplyKeyboardRemove

from src.bot.keyboards import (
    _back_kb,
    _category_view_keyboard,
    _cat_view_text,
    _confirm_keyboard,
    _source_view_keyboard,
)
from src.bot.state import _pending
from src.collectors.folder_manager import add_to_folder, remove_from_folder
from src.collectors.telegram_collector import load_watched_channels, userbot
from src.config import settings
from src.db.models import (
    add_category,
    add_source,
    category_exists,
    get_active_sources,
    get_categories,
    remove_source,
)

log = logging.getLogger(__name__)


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


async def _send_sources_list(target, reply: bool) -> None:
    cats = await get_categories()
    if not cats:
        text = "No categories yet."
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("➕ Add source", callback_data="src_add:")]])
        if reply:
            await target.reply(text, reply_markup=kb)
        else:
            await target.edit_text(text, reply_markup=kb)
        return

    all_sources = await get_active_sources()
    src_count: dict[str, int] = {}
    for s in all_sources:
        src_count[s["category"]] = src_count.get(s["category"], 0) + 1

    buttons = []
    for c in cats:
        count = src_count.get(c["name"], 0)
        label = f"{c['emoji']} {c['name']}  ({count})" if count else f"{c['emoji']} {c['name']}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"cat_view:{c['name']}")])
    buttons.append([InlineKeyboardButton("➕ Add source", callback_data="src_add:")])
    kb = InlineKeyboardMarkup(buttons)
    if reply:
        await target.reply("Sources:", reply_markup=kb)
    else:
        await target.edit_text("Sources:", reply_markup=kb)


def register_source_handlers(bot, admin_msg, admin_cb) -> None:

    @bot.on_message(pf.command("sources") & admin_msg)
    async def cmd_sources(_, message: Message) -> None:
        await _send_sources_list(message, reply=True)

    @bot.on_callback_query(pf.regex(r"^src_list$") & admin_cb)
    async def cb_src_list(_, query: CallbackQuery) -> None:
        _pending.pop(query.from_user.id, None)
        await _send_sources_list(query.message, reply=False)

    @bot.on_callback_query(pf.regex(r"^src_view:") & admin_cb)
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

    @bot.on_callback_query(pf.regex(r"^src_reassign:") & admin_cb)
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

    @bot.on_callback_query(pf.regex(r"^src_reassign_to:") & admin_cb)
    async def cb_src_reassign_to(_, query: CallbackQuery) -> None:
        _, src_id_str, cat_name = query.data.split(":", 2)
        src_id = int(src_id_str)
        async with aiosqlite.connect(settings.database_path) as db:
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

    @bot.on_callback_query(pf.regex(r"^src_del:") & admin_cb)
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

    @bot.on_callback_query(pf.regex(r"^src_del_ok:") & admin_cb)
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
            await query.message.edit_text(
                "✅ Source removed.\n\n" + text,
                reply_markup=_category_view_keyboard(cat_name, remaining),
            )
        else:
            await query.message.edit_text("✅ Source removed.")

    @bot.on_callback_query(pf.regex(r"^src_add:") & admin_cb)
    async def cb_src_add(_, query: CallbackQuery) -> None:
        cat_name = query.data.split(":", 1)[1]
        uid = query.from_user.id
        data: dict = {}
        if cat_name:
            data["preset_category"] = cat_name
        _pending[uid] = {"action": "add_source", "step": 0, "data": data}
        back = f"cat_view:{cat_name}" if cat_name else "src_list"
        prompt = f"Adding source to <b>{cat_name}</b>.\n\nURL or @channel:" if cat_name else "URL or @channel:"
        await query.message.edit_text(prompt, reply_markup=_back_kb(back))

    @bot.on_callback_query(pf.regex(r"^add_src_cat:") & admin_cb)
    async def cb_add_src_cat(_, query: CallbackQuery) -> None:
        cat = query.data.split(":", 1)[1]
        uid = query.from_user.id
        if uid not in _pending or _pending[uid].get("action") != "add_source":
            await query.answer("Session expired. Use /sources again.", show_alert=True)
            return
        data = _pending[uid]["data"]
        await query.message.edit_text(f"Name: <b>{data['name']}</b>\nCategory: <b>{cat}</b>")
        await _finalize_add_source(uid, cat, data, query.message, reply=False)

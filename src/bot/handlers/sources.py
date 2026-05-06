import logging
from html import escape

from pyrogram import filters as pf
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove

from src.bot.keyboards import (
    _back_kb,
    _category_view_keyboard,
    _cat_view_text,
    _confirm_keyboard,
    _source_view_keyboard,
)
from src.bot.state import _DEFAULT_DIGEST_TIME, _pending
from src.collectors.folder_manager import add_to_folder, remove_from_folder
from src.collectors.telegram_collector import userbot
from src.db.models import (
    add_category,
    add_source,
    category_exists,
    get_categories,
    get_source,
    reassign_source_category,
    remove_source,
    rename_source,
    reorder_source,
)

log = logging.getLogger(__name__)


async def _finalize_add_source(uid: int, cat: str, data: dict, message, reply: bool = True) -> None:
    source_type = data["type"]
    url = data["url"]
    name = data["name"]
    cat = cat.lower()
    if not await category_exists(cat):
        await add_category(cat, "📌", _DEFAULT_DIGEST_TIME)
        log.info("Auto-created category: 📌 %s", cat)

    status = "active"
    extra = ""
    if source_type == "telegram":
        raw = url.lstrip("@")
        try:
            await userbot.join_chat(raw)
            log.info("Userbot joined %s", url)
            await add_to_folder(raw)
        except Exception as exc:
            log.warning("Could not join %s: %s", url, exc)
            status = "pending"
            extra = "\n⏳ Could not join — saved as pending"

    await add_source(source_type, name, url, cat, status=status)

    del _pending[uid]
    log.info("Source added: [%s] %s (%s) -> category=%s status=%s", source_type, name, url, cat, status)
    prefix = "⏳" if status == "pending" else "✅"
    text = (
        f"{prefix} Added [{source_type}] <b>{name}</b> — <code>{url}</code>\n"
        f"Category: <b>{cat}</b>{extra}"
    )
    if reply:
        await message.reply(text, reply_markup=ReplyKeyboardRemove())
    else:
        await message.reply(text)


def register_source_handlers(bot, admin_msg, admin_cb) -> None:

    @bot.on_callback_query(pf.regex(r"^src_view:") & admin_cb)
    async def cb_src_view(_, query: CallbackQuery) -> None:
        src_id = int(query.data.split(":", 1)[1])
        s = await get_source(src_id)
        if not s:
            await query.answer("Source not found.", show_alert=True)
            return
        pending = s["status"] == "pending"
        icon = "⏳" if pending else ("📡" if s["type"] == "telegram" else "🔗")
        type_label = "tg" if s["type"] == "telegram" else "rss"
        status_line = "\nStatus: <b>pending</b>" if pending else ""
        text = (
            f"{icon} <b>{s['name']}</b>\n"
            f"Type: <code>{type_label}</code>\n"
            f"URL: <code>{s['url']}</code>\n"
            f"Category: <b>{s['category']}</b>{status_line}"
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
        await reassign_source_category(src_id, cat_name)
        log.info("Source id=%s reassigned to category=%s", src_id, cat_name)
        s = await get_source(src_id)
        if s:
            pending = s["status"] == "pending"
            icon = "⏳" if pending else ("📡" if s["type"] == "telegram" else "🔗")
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
        s = await get_source(src_id)
        name = s["name"] if s else str(src_id)
        cat = s["category"] if s else ""
        await query.message.edit_text(
            f"Remove source <b>{name}</b>?",
            reply_markup=_confirm_keyboard(f"src_del_ok:{src_id}", f"cat_view:{cat}"),
        )

    @bot.on_callback_query(pf.regex(r"^src_del_ok:") & admin_cb)
    async def cb_src_del_ok(_, query: CallbackQuery) -> None:
        src_id = int(query.data.split(":", 1)[1])
        s = await get_source(src_id)
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

    @bot.on_callback_query(pf.regex(r"^src_rename:") & admin_cb)
    async def cb_src_rename(_, query: CallbackQuery) -> None:
        src_id = int(query.data.split(":", 1)[1])
        s = await get_source(src_id)
        if not s:
            await query.answer("Source not found.", show_alert=True)
            return
        uid = query.from_user.id
        _pending[uid] = {
            "action": "rename_source",
            "step": 0,
            "data": {"source_id": src_id, "cat_name": s["category"]},
        }
        await query.message.edit_text(
            f"Rename <b>{escape(s['name'])}</b>.\n\nNew name:",
            reply_markup=_back_kb(f"src_view:{src_id}"),
        )

    @bot.on_callback_query(pf.regex(r"^src_add:") & admin_cb)
    async def cb_src_add(_, query: CallbackQuery) -> None:
        cat_name = query.data.split(":", 1)[1]
        uid = query.from_user.id
        data: dict = {}
        if cat_name:
            data["preset_category"] = cat_name
        _pending[uid] = {"action": "add_source", "step": 0, "data": data}
        back = f"cat_view:{cat_name}" if cat_name else "cat_list"
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

    @bot.on_callback_query(pf.regex(r"^src_order_(up|down):") & admin_cb)
    async def cb_src_order(_, query: CallbackQuery) -> None:
        parts = query.data.split(":", 2)
        direction = "up" if "up" in parts[0] else "down"
        src_id = int(parts[1])
        cat_name = parts[2]
        await reorder_source(src_id, cat_name, direction)
        text, sources = await _cat_view_text(cat_name)
        await query.message.edit_text(text, reply_markup=_category_view_keyboard(cat_name, sources))

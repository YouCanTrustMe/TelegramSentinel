import logging

from pyrogram import filters as pf
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from src.bot.keyboards import (
    _back_kb,
    _cancel_kb,
    _cat_edit_keyboard,
    _categories_keyboard,
    _category_view_keyboard,
    _cat_view_text,
    _confirm_keyboard,
    _edit_time_kb,
    _time_step_kb,
)
from src.bot.state import _DEFAULT_DIGEST_TIME, _pending
from src.db.models import (
    add_category,
    delete_sources_by_category,
    get_categories,
    get_sources_by_category,
    move_sources_to_category,
    remove_category,
    update_category,
)
from src.scheduler import rebuild_digest_jobs

log = logging.getLogger(__name__)


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


def register_category_handlers(bot, admin_msg, admin_cb) -> None:

    @bot.on_message(pf.command("categories") & admin_msg)
    async def cmd_categories(_, message: Message) -> None:
        cats = await get_categories()
        if not cats:
            await message.reply("No categories yet.", reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("➕ Add category", callback_data="cat_add")]]
            ))
            return
        await message.reply("Categories:", reply_markup=await _categories_keyboard(cats))

    @bot.on_callback_query(pf.regex(r"^cat_list$") & admin_cb)
    async def cb_cat_list(_, query: CallbackQuery) -> None:
        _pending.pop(query.from_user.id, None)
        cats = await get_categories()
        if not cats:
            await query.message.edit_text("No categories yet.", reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("➕ Add category", callback_data="cat_add")]]
            ))
            return
        await query.message.edit_text("Categories:", reply_markup=await _categories_keyboard(cats))

    @bot.on_callback_query(pf.regex(r"^cat_view:") & admin_cb)
    async def cb_cat_view(_, query: CallbackQuery) -> None:
        _pending.pop(query.from_user.id, None)
        cat_name = query.data.split(":", 1)[1]
        text, sources = await _cat_view_text(cat_name)
        await query.message.edit_text(text, reply_markup=_category_view_keyboard(cat_name, sources))

    @bot.on_callback_query(pf.regex(r"^cat_add$") & admin_cb)
    async def cb_cat_add(_, query: CallbackQuery) -> None:
        uid = query.from_user.id
        _pending[uid] = {"action": "add_category", "step": 0, "data": {}}
        await query.message.edit_text("Category name:", reply_markup=_cancel_kb())

    @bot.on_callback_query(pf.regex(r"^cat_add_time_default$") & admin_cb)
    async def cb_cat_add_time_default(_, query: CallbackQuery) -> None:
        uid = query.from_user.id
        if uid not in _pending or _pending[uid].get("action") != "add_category":
            await query.answer("Session expired.", show_alert=True)
            return
        data = _pending[uid]["data"]
        data["digest_time"] = _DEFAULT_DIGEST_TIME
        await _finalize_add_category(uid, data, query.message, reply=False)

    @bot.on_callback_query(pf.regex(r"^cat_del:") & admin_cb)
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

    @bot.on_callback_query(pf.regex(r"^cat_del_ok:") & admin_cb)
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
        await query.message.edit_text("✅ Category removed.\n\nCategories:", reply_markup=await _categories_keyboard(cats))

    @bot.on_callback_query(pf.regex(r"^cat_del_move:") & admin_cb)
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
            await query.message.edit_text(text, reply_markup=await _categories_keyboard(cats))

    @bot.on_callback_query(pf.regex(r"^cat_del_all:") & admin_cb)
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
        await query.message.edit_text("✅ Category and all sources deleted.\n\nCategories:", reply_markup=await _categories_keyboard(cats))

    @bot.on_callback_query(pf.regex(r"^cat_edit:") & admin_cb)
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

    @bot.on_callback_query(pf.regex(r"^cat_edit_field:") & admin_cb)
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

import logging

from pyrogram import filters as pf
from pyrogram.types import CallbackQuery, Message

from src.bot.keyboards import _edit_time_kb, _timetable_keyboard, _timetable_slots, _timetable_text
from src.bot.state import _pending
from src.db.models import get_categories

log = logging.getLogger(__name__)


def register_timetable_handlers(bot, admin_msg, admin_cb) -> None:

    @bot.on_message(pf.command("timetable") & admin_msg)
    async def cmd_timetable(_, message: Message) -> None:
        cats = await get_categories()
        log.info("Timetable opened | slots=%s", list(_timetable_slots(cats)))
        await message.reply(_timetable_text(cats), reply_markup=_timetable_keyboard(cats))

    @bot.on_callback_query(pf.regex(r"^tt_list$") & admin_cb)
    async def cb_tt_list(_, query: CallbackQuery) -> None:
        _pending.pop(query.from_user.id, None)
        cats = await get_categories()
        log.debug("Timetable reopened | slots=%s", list(_timetable_slots(cats)))
        await query.message.edit_text(_timetable_text(cats), reply_markup=_timetable_keyboard(cats))

    @bot.on_callback_query(pf.regex(r"^tt_edit:") & admin_cb)
    async def cb_tt_edit(_, query: CallbackQuery) -> None:
        cat_name = query.data.split(":", 1)[1]
        cats = await get_categories()
        cat = next((c for c in cats if c["name"] == cat_name), None)
        if cat is None:
            log.warning("Timetable edit for unknown category=%s", cat_name)
            await query.answer("Category not found.", show_alert=True)
            return
        log.info("Timetable: awaiting new digest time for category=%s | current=%s", cat_name, cat["digest_time"])
        # Reuses the edit_category/time conversation branch, which now answers with
        # the timetable rather than the category view.
        _pending[query.from_user.id] = {
            "action": "edit_category",
            "step": 0,
            "data": {"cat_name": cat_name, "field": "time"},
        }
        await query.message.edit_text(
            f"New digest time for <b>{cat['emoji']} {cat_name}</b> "
            f"(HH:MM or comma-separated):\nCurrent: <b>{cat['digest_time']}</b>",
            reply_markup=_edit_time_kb(),
        )

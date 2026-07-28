import logging

from pyrogram import filters as pf
from pyrogram.types import CallbackQuery

from src.bot.keyboards import (
    _add_time_kb,
    _confirm_keyboard,
    _slot_keyboard,
    _slot_text,
    _timetable_keyboard,
    _timetable_text,
)
from src.bot.state import _pending
from src.common.schedule import fires_at, slots_by_time, with_time, without_time
from src.db.models import get_categories, update_category
from src.scheduler import rebuild_digest_jobs

log = logging.getLogger(__name__)


async def _show_timetable(query: CallbackQuery) -> None:
    cats = await get_categories()
    await query.message.edit_text(_timetable_text(cats), reply_markup=_timetable_keyboard(cats))


async def _show_slot(query: CallbackQuery, time_str: str) -> None:
    cats = await get_categories()
    await query.message.edit_text(_slot_text(time_str, cats), reply_markup=_slot_keyboard(time_str, cats))


def _split_toggle_data(data: str) -> tuple[str, str]:
    """Pull (time, category) out of "tt_toggle:HH:MM:name". The time carries a colon
    of its own, so the category is taken from the right — splitting left-to-right
    yields time="HH" and category="MM:name"."""
    return data.split(":", 1)[1].rsplit(":", 1)


def _orphans(cats, time_str: str) -> list[str]:
    """Categories whose only digest time is `time_str`. Removing it would leave them
    with no schedule at all — there is no catch-all job, so their items would never
    be sent and would pile up unsent forever."""
    return [c["name"] for c in cats if fires_at(c, time_str) and not without_time(c["digest_time"], time_str)]


def register_timetable_handlers(bot, admin_msg, admin_cb) -> None:

    @bot.on_callback_query(pf.regex(r"^tt_list$") & admin_cb)
    async def cb_tt_list(_, query: CallbackQuery) -> None:
        _pending.pop(query.from_user.id, None)
        cats = await get_categories()
        log.info("Timetable opened | slots=%s", list(slots_by_time(cats)))
        await query.message.edit_text(_timetable_text(cats), reply_markup=_timetable_keyboard(cats))

    @bot.on_callback_query(pf.regex(r"^tt_slot:") & admin_cb)
    async def cb_tt_slot(_, query: CallbackQuery) -> None:
        _pending.pop(query.from_user.id, None)
        time_str = query.data.split(":", 1)[1]
        log.info("Timetable slot opened | time=%s", time_str)
        await _show_slot(query, time_str)

    @bot.on_callback_query(pf.regex(r"^tt_toggle:") & admin_cb)
    async def cb_tt_toggle(_, query: CallbackQuery) -> None:
        time_str, cat_name = _split_toggle_data(query.data)
        cats = await get_categories()
        cat = next((c for c in cats if c["name"] == cat_name), None)
        if cat is None:
            log.warning("Timetable toggle for unknown category=%s | time=%s", cat_name, time_str)
            await query.answer("Category not found.", show_alert=True)
            return

        if fires_at(cat, time_str):
            new_times = without_time(cat["digest_time"], time_str)
            if not new_times:
                log.info("Timetable toggle refused | category=%s time=%s | it is the only one left", cat_name, time_str)
                await query.answer(
                    f"{cat_name} has no other digest time — it would never be sent. Add another time first.",
                    show_alert=True,
                )
                return
        else:
            new_times = with_time(cat["digest_time"], time_str)

        await update_category(cat_name, new_digest_time=new_times)
        await rebuild_digest_jobs()
        log.info("Timetable toggle | category=%s time=%s | digest_time=%s", cat_name, time_str, new_times)
        await _show_slot(query, time_str)

    @bot.on_callback_query(pf.regex(r"^tt_add$") & admin_cb)
    async def cb_tt_add(_, query: CallbackQuery) -> None:
        cats = await get_categories()
        slots = list(slots_by_time(cats))
        _pending[query.from_user.id] = {"action": "add_digest_time", "step": 0, "data": {}}
        log.info("Timetable awaiting a new time | existing=%s", slots)
        existing = " · ".join(slots) if slots else "none"
        await query.message.edit_text(
            "🕐 <b>New digest time</b>\n\n<i>Send it as HH:MM, e.g. 08:30.</i>\n\n"
            f"<i>Existing: {existing}</i>",
            reply_markup=_add_time_kb(),
        )

    @bot.on_callback_query(pf.regex(r"^tt_del:") & admin_cb)
    async def cb_tt_del(_, query: CallbackQuery) -> None:
        time_str = query.data.split(":", 1)[1]
        cats = await get_categories()
        orphans = _orphans(cats, time_str)
        if orphans:
            log.info("Timetable remove refused | time=%s | would orphan=%s", time_str, orphans)
            await query.answer(
                f"{', '.join(orphans)} would be left with no digest time. Give them another time first.",
                show_alert=True,
            )
            return
        affected = [c for c in cats if fires_at(c, time_str)]
        names = ", ".join(f"{c['emoji']} {c['name']}" for c in affected)
        await query.message.edit_text(
            f"🗑 <b>Remove {time_str}?</b>\n\n<i>{names} will stop going out at {time_str}.</i>",
            reply_markup=_confirm_keyboard(f"tt_del_ok:{time_str}", f"tt_slot:{time_str}"),
        )

    @bot.on_callback_query(pf.regex(r"^tt_del_ok:") & admin_cb)
    async def cb_tt_del_ok(_, query: CallbackQuery) -> None:
        time_str = query.data.split(":", 1)[1]
        cats = await get_categories()
        if _orphans(cats, time_str):
            await query.answer("Schedule changed meanwhile — nothing removed.", show_alert=True)
            await _show_timetable(query)
            return
        removed = 0
        for cat in cats:
            if fires_at(cat, time_str):
                await update_category(cat["name"], new_digest_time=without_time(cat["digest_time"], time_str))
                removed += 1
        await rebuild_digest_jobs()
        log.info("Timetable time removed | time=%s categories=%d", time_str, removed)
        await _show_timetable(query)

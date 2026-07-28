import logging
from html import escape

from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from src.bot.state import _DEFAULT_DIGEST_TIME
from src.common.schedule import fires_at, parse_times, slots_by_time
from src.db.models import get_active_sources, get_categories, get_pending_sources

log = logging.getLogger(__name__)

_PAGE_SIZE_CATS = 8
_PAGE_SIZE_SOURCES = 8
_PAGE_SIZE_BLOCKED = 7


def _is_rss(url: str) -> bool:
    return url.startswith("http://") or url.startswith("https://")


def _back_kb(back_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀ Back", callback_data=back_data)]])


async def _categories_keyboard(cats, page: int = 0) -> InlineKeyboardMarkup:
    all_sources = await get_active_sources()
    pending = await get_pending_sources()
    src_count: dict[str, int] = {}
    for s in list(all_sources) + list(pending):
        src_count[s["category"]] = src_count.get(s["category"], 0) + 1

    total = len(cats)
    start = page * _PAGE_SIZE_CATS
    page_cats = cats[start:start + _PAGE_SIZE_CATS]
    total_pages = max(1, (total + _PAGE_SIZE_CATS - 1) // _PAGE_SIZE_CATS)

    buttons = []
    for i, r in enumerate(page_cats):
        global_idx = start + i
        count = src_count.get(r["name"], 0)
        label = f"{r['emoji']} {r['name']}  ({count})" if count else f"{r['emoji']} {r['name']}"
        buttons.append([
            InlineKeyboardButton("↑" if global_idx > 0 else " ", callback_data=f"cat_order_up:{r['name']}" if global_idx > 0 else "noop"),
            InlineKeyboardButton(label, callback_data=f"cat_view:{r['name']}"),
            InlineKeyboardButton("↓" if global_idx < total - 1 else " ", callback_data=f"cat_order_down:{r['name']}" if global_idx < total - 1 else "noop"),
        ])

    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀", callback_data=f"cat_list:{page - 1}"))
        nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton("▶", callback_data=f"cat_list:{page + 1}"))
        buttons.append(nav)

    buttons.append([
        InlineKeyboardButton("➕ Add category", callback_data="cat_add"),
        InlineKeyboardButton("🕐 Timetable", callback_data="tt_list"),
    ])
    return InlineKeyboardMarkup(buttons)


def _category_view_keyboard(cat_name: str, sources, page: int = 0) -> InlineKeyboardMarkup:
    total = len(sources)
    start = page * _PAGE_SIZE_SOURCES
    page_sources = sources[start:start + _PAGE_SIZE_SOURCES]
    total_pages = max(1, (total + _PAGE_SIZE_SOURCES - 1) // _PAGE_SIZE_SOURCES)

    buttons = []
    for i, s in enumerate(page_sources):
        global_idx = start + i
        pending = s["status"] == "pending"
        icon = "⏳" if pending else ("📡" if s["type"] == "telegram" else "🔗")
        type_label = "tg" if s["type"] == "telegram" else "rss"
        buttons.append([
            InlineKeyboardButton("↑" if global_idx > 0 else " ", callback_data=f"src_order_up:{s['id']}:{cat_name}" if global_idx > 0 else "noop"),
            InlineKeyboardButton(f"{icon} [{type_label}] {s['name']}", callback_data=f"src_view:{s['id']}"),
            InlineKeyboardButton("↓" if global_idx < total - 1 else " ", callback_data=f"src_order_down:{s['id']}:{cat_name}" if global_idx < total - 1 else "noop"),
        ])

    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀", callback_data=f"cat_view:{cat_name}:{page - 1}"))
        nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton("▶", callback_data=f"cat_view:{cat_name}:{page + 1}"))
        buttons.append(nav)

    buttons.append([
        InlineKeyboardButton("➕ Add source", callback_data=f"src_add:{cat_name}"),
        InlineKeyboardButton("✏️ Edit", callback_data=f"cat_edit:{cat_name}"),
        InlineKeyboardButton("🗑 Delete", callback_data=f"cat_del:{cat_name}"),
    ])
    buttons.append([InlineKeyboardButton("📝 Bulk prompt", callback_data=f"cat_bulk_prompt:{cat_name}")])
    buttons.append([InlineKeyboardButton("◀ Back", callback_data="cat_list")])
    return InlineKeyboardMarkup(buttons)


def _cat_edit_keyboard(cat_name: str) -> InlineKeyboardMarkup:
    # Digest time is edited in the timetable, where the whole day is visible at once.
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Rename", callback_data=f"cat_edit_field:{cat_name}:name")],
        [InlineKeyboardButton("🎨 Change emoji", callback_data=f"cat_edit_field:{cat_name}:emoji")],
        [InlineKeyboardButton("🕐 Timetable", callback_data="tt_list")],
        [InlineKeyboardButton("◀ Back", callback_data=f"cat_view:{cat_name}")],
    ])


def _source_view_keyboard(source_id: int, cat_name: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Rename", callback_data=f"src_rename:{source_id}")],
        [InlineKeyboardButton("📝 Prompt", callback_data=f"src_prompt:{source_id}")],
        [InlineKeyboardButton("🔄 Reassign category", callback_data=f"src_reassign:{source_id}")],
        [InlineKeyboardButton("🗑 Remove source", callback_data=f"src_del:{source_id}")],
        [InlineKeyboardButton("◀ Back", callback_data=f"cat_view:{cat_name}")],
    ])


def _confirm_keyboard(yes_data: str, no_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes", callback_data=yes_data),
        InlineKeyboardButton("❌ No", callback_data=no_data),
    ]])


def _timetable_text(cats) -> str:
    """Categories sharing a schedule are shown as one entry: with most categories
    on the same times, listing all of them per slot buried the one that differs."""
    if not cats:
        return "🕐 <b>Timetable</b>\n\nNo categories yet."

    slots = slots_by_time(cats)
    groups: dict[str, list] = {}
    never: list = []
    for cat in cats:
        key = " · ".join(f"{h:02d}:{m:02d}" for h, m in sorted(set(parse_times(cat["digest_time"]))))
        if key:
            groups.setdefault(key, []).append(cat)
        else:
            never.append(cat)

    def _names(group) -> str:
        return "   ".join(f"{c['emoji']} {escape(c['name'])}" for c in group)

    lines = ["🕐 <b>Timetable</b>", ""]
    for key, group in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        lines.append(f"<b>{key}</b>")
        lines.append(_names(group))
        lines.append("")
    if never:
        # Listed rather than skipped: an unscheduled category is exactly what the
        # reader needs to see here, and it has no other screen that would show it.
        lines.append("<b>⚠️ never</b>")
        lines.append(_names(never))
        lines.append("")
    if slots:
        # The last slot of the day also carries the quiet-sources block (see _rebuild_jobs).
        lines.append(f"<i>⏸ quiet sources ride with the {list(slots)[-1]} digest</i>")
    return "\n".join(lines)


def _timetable_keyboard(cats) -> InlineKeyboardMarkup:
    slots = list(slots_by_time(cats))
    buttons = []
    for i in range(0, len(slots), 3):
        buttons.append([
            InlineKeyboardButton(t, callback_data=f"tt_slot:{t}") for t in slots[i:i + 3]
        ])
    buttons.append([InlineKeyboardButton("➕ Add time", callback_data="tt_add")])
    buttons.append([InlineKeyboardButton("◀ Back", callback_data="cat_list")])
    return InlineKeyboardMarkup(buttons)


def _slot_text(time_str: str, cats) -> str:
    on = sum(1 for c in cats if fires_at(c, time_str))
    if not on:
        return (
            f"🕐 <b>{time_str}</b>\n\n"
            f"<i>Nothing goes out at {time_str} yet — pick the categories below. "
            f"The time disappears again if you leave it empty.</i>"
        )
    return f"🕐 <b>{time_str}</b>\n\n<i>{on} of {len(cats)} categories go out at {time_str}.</i>"


def _slot_keyboard(time_str: str, cats) -> InlineKeyboardMarkup:
    buttons = []
    for i in range(0, len(cats), 2):
        buttons.append([
            InlineKeyboardButton(
                f"{'✅' if fires_at(c, time_str) else '⬜'} {c['emoji']} {c['name']}",
                callback_data=f"tt_toggle:{time_str}:{c['name']}",
            )
            for c in cats[i:i + 2]
        ])
    if any(fires_at(c, time_str) for c in cats):
        buttons.append([InlineKeyboardButton(f"🗑 Remove {time_str}", callback_data=f"tt_del:{time_str}")])
    buttons.append([InlineKeyboardButton("◀ Back", callback_data="tt_list")])
    return InlineKeyboardMarkup(buttons)


def _add_time_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="tt_list")]])


def _blocked_keyboard(words, page: int = 0) -> InlineKeyboardMarkup:
    total = len(words)
    start = page * _PAGE_SIZE_BLOCKED
    page_words = words[start:start + _PAGE_SIZE_BLOCKED]
    total_pages = max(1, (total + _PAGE_SIZE_BLOCKED - 1) // _PAGE_SIZE_BLOCKED)

    buttons = []
    for w in page_words:
        buttons.append([
            InlineKeyboardButton(f"🔴 {w['rule']}", callback_data=f"blocked_view:{w['id']}"),
        ])

    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀", callback_data=f"blocked_list:{page - 1}"))
        nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton("▶", callback_data=f"blocked_list:{page + 1}"))
        buttons.append(nav)

    buttons.append([InlineKeyboardButton("➕ Add filter", callback_data="blocked_add")])
    return InlineKeyboardMarkup(buttons)


async def _cat_view_text(cat_name: str) -> tuple[str, list]:
    cats = await get_categories()
    active = [s for s in await get_active_sources() if s["category"] == cat_name]
    pending = list(await get_pending_sources(cat_name))
    cat = next((c for c in cats if c["name"] == cat_name), None)
    emoji = cat["emoji"] if cat else "📌"
    digest_time = cat["digest_time"] if cat else _DEFAULT_DIGEST_TIME
    all_sources = sorted(active + pending, key=lambda s: (s["sort_order"], s["name"]))
    count = len(all_sources)
    if not all_sources:
        text = f"<b>{emoji} {cat_name}</b>  ·  ⏰ {digest_time}\n\nNo sources yet."
    else:
        label = "source" if count == 1 else "sources"
        text = f"<b>{emoji} {cat_name}</b>  ·  ⏰ {digest_time}  ·  {count} {label}"
    return text, all_sources

import logging
from html import escape

from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from src.bot.state import _DEFAULT_DIGEST_TIME
from src.db.models import get_active_sources, get_categories, get_pending_sources

log = logging.getLogger(__name__)

_PAGE_SIZE_CATS = 8
_PAGE_SIZE_SOURCES = 8
_PAGE_SIZE_BLOCKED = 7


def _is_rss(url: str) -> bool:
    return url.startswith("http://") or url.startswith("https://")


def _is_valid_time(t: str) -> bool:
    parts = [p.strip() for p in t.split(",")]
    if not parts or not parts[0]:
        return False
    for part in parts:
        try:
            h, m = map(int, part.split(":"))
            if not (0 <= h < 24 and 0 <= m < 60):
                return False
        except (ValueError, AttributeError):
            return False
    return True


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

    buttons.append([InlineKeyboardButton("➕ Add category", callback_data="cat_add")])
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
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Rename", callback_data=f"cat_edit_field:{cat_name}:name")],
        [InlineKeyboardButton("🎨 Change emoji", callback_data=f"cat_edit_field:{cat_name}:emoji")],
        [InlineKeyboardButton("🕐 Change digest time", callback_data=f"cat_edit_field:{cat_name}:time")],
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


def _time_step_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"⏭ Default ({_DEFAULT_DIGEST_TIME})", callback_data="cat_add_time_default")],
        [InlineKeyboardButton("◀ Back", callback_data="cat_list")],
    ])


def _edit_time_kb(back_cat: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel", callback_data=f"cat_edit:{back_cat}")],
    ])


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

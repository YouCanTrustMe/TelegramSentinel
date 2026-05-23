"""Shared presentation helpers for the radar handlers: the main menu keyboard,
the generic paginated list keyboard, row labels and the keyword/chat list
renderers reused across the radar_* modules."""
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from src.db.models import get_keyword_chat_links, get_radar_chats, get_radar_keywords

_PAGE_SIZE = 10


def _radar_main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📋 Keywords", callback_data="radar_keywords:0"),
            InlineKeyboardButton("🎯 Watchlist", callback_data="radar_chats:0"),
        ],
        [
            InlineKeyboardButton("🚫 Blacklist", callback_data="radar_blacklist:0"),
            InlineKeyboardButton("📊 Status", callback_data="radar_status"),
        ],
    ])


def _radar_list_kb(
    items,
    page: int,
    id_field: str,
    del_prefix: str,
    add_cb: str,
    list_cb_base: str,
    label_fn,
    view_prefix: str | None = None,
) -> InlineKeyboardMarkup:
    total = len(items)
    start = page * _PAGE_SIZE
    page_items = items[start : start + _PAGE_SIZE]
    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)

    buttons = []
    for row in page_items:
        label_cb = f"{view_prefix}{row[id_field]}" if view_prefix else "noop"
        buttons.append([
            InlineKeyboardButton(label_fn(row), callback_data=label_cb),
            InlineKeyboardButton("❌", callback_data=f"{del_prefix}{row[id_field]}"),
        ])

    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("◀", callback_data=f"{list_cb_base}:{page - 1}"))
        nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton("▶", callback_data=f"{list_cb_base}:{page + 1}"))
        buttons.append(nav)

    buttons.append([InlineKeyboardButton("➕ Add", callback_data=add_cb)])
    buttons.append([InlineKeyboardButton("◀ Back", callback_data="radar_main")])
    return InlineKeyboardMarkup(buttons)


def _chat_label(row, kw_count: int | None = None) -> str:
    status = row["status"] if "status" in row.keys() else "active"
    prefix = "⚠️ " if status != "active" else ""
    base = f"{row['title']} ({row['chat_ref']})" if row["title"] else row["chat_ref"]
    suffix = ""
    if kw_count is not None:
        suffix = " — ⚠️ unbound" if kw_count == 0 else f" · {kw_count}kw"
    return f"{prefix}{base}{suffix}"


def _kw_label(row, chat_count: int | None = None) -> str:
    base = row["keyword"]
    if chat_count is None:
        return base
    return f"⚠️ {base} — unbound" if chat_count == 0 else f"{base} · {chat_count}ch"


async def _render_keywords(page: int) -> tuple[str, InlineKeyboardMarkup]:
    items = await get_radar_keywords()
    links = await get_keyword_chat_links()
    kw_counts: dict[int, int] = {}
    for link in links:
        kw_counts[link["keyword_id"]] = kw_counts.get(link["keyword_id"], 0) + 1
    text = (
        f"📋 <b>Keywords</b> ({len(items)})"
        if items
        else "📋 <b>Keywords</b>\n\nNo keywords yet."
    )
    kb = _radar_list_kb(
        items, page, "id", "radar_kw_del:", "radar_kw_add",
        "radar_keywords",
        lambda r: _kw_label(r, kw_counts.get(r["id"], 0)),
        view_prefix="radar_kw_view:",
    )
    return text, kb


async def _render_chats(page: int) -> tuple[str, InlineKeyboardMarkup]:
    items = await get_radar_chats()
    links = await get_keyword_chat_links()
    chat_counts: dict[int, int] = {}
    for link in links:
        chat_counts[link["chat_id"]] = chat_counts.get(link["chat_id"], 0) + 1
    text = (
        f"💬 <b>Monitored chats</b> ({len(items)})"
        if items
        else "💬 <b>Monitored chats</b>\n\nNo chats yet."
    )
    kb = _radar_list_kb(
        items, page, "id", "radar_chat_del:", "radar_chat_add",
        "radar_chats",
        lambda r: _chat_label(r, chat_counts.get(r["id"], 0)),
        view_prefix="radar_chat_view:",
    )
    return text, kb

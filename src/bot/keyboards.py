import logging
from html import escape

from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from src.bot.state import _DEFAULT_DIGEST_TIME
from src.db.models import get_active_sources, get_categories

log = logging.getLogger(__name__)


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


def _blocked_keyboard(words) -> InlineKeyboardMarkup:
    buttons = []
    for w in words:
        buttons.append([
            InlineKeyboardButton(f"🔴 {w['word']}", callback_data="noop"),
            InlineKeyboardButton("🗑", callback_data=f"blocked_del:{w['id']}"),
        ])
    buttons.append([InlineKeyboardButton("➕ Add word", callback_data="blocked_add")])
    return InlineKeyboardMarkup(buttons)


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

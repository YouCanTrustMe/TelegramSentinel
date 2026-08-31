import logging
from html import escape

from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from src.bot.state import _DEFAULT_DIGEST_TIME
from src.common.schedule import fires_at, parse_times, slots_by_time
from src.common.util import ago, source_link
from src.db.models import (
    get_active_sources,
    get_blocked_hit_counts,
    get_categories,
    get_error_sources,
    get_pending_sources,
    get_silent_sources,
    get_word_category_map,
)

log = logging.getLogger(__name__)

_PAGE_SIZE_CATS = 8
_PAGE_SIZE_SOURCES = 8
_PAGE_SIZE_BLOCKED = 7


# Telegram clips a longer inline label itself, mid-word and without an ellipsis.
_LABEL_MAX = 40
_QUIET_THRESHOLD_HOURS = 120


def page_of(items, key: str, value, page_size: int) -> int:
    """Page the row now sits on. A move across a page boundary used to re-render
    the old page, where the row had just disappeared from."""
    for index, row in enumerate(items):
        if row[key] == value:
            return index // page_size
    return 0


def split_name_page(payload: str) -> tuple[str, int]:
    """Read "name" / "name:2" where the name may itself contain a colon. Parsing
    from the left made every tap on a category named "a:b" raise ValueError."""
    name, sep, tail = payload.rpartition(":")
    if sep and tail.isdigit():
        return name, int(tail)
    return payload, 0


def _clip(label: str, limit: int = _LABEL_MAX) -> str:
    """Trim a button label at a word boundary and say so, rather than letting
    Telegram cut "Financial Times · 12 · 09:00" into "Financial Times · 12 · 09"."""
    if len(label) <= limit:
        return label
    head = label[:limit - 1]
    cut = head.rsplit(" ", 1)[0] if " " in head[limit // 2:] else head
    return f"{cut.rstrip(' ·-')}…"


def _times_label(digest_time: str) -> str:
    """"09:00,14:00" -> "09:00 14:00"; an unscheduled category says so, because
    this list is the only place its silence would otherwise be invisible."""
    times = sorted(set(parse_times(digest_time or "")))
    if not times:
        return "⚠️ never"
    return " ".join(f"{h:02d}:{m:02d}" for h, m in times)


def _category_label(cat, source_count: int, quiet_count: int = 0) -> str:
    """A category row answers the two questions this screen is opened with: how
    many sources, and when does it go out."""
    quiet = f" ⏸{quiet_count}" if quiet_count else ""
    return _clip(f"{cat['emoji']} {cat['name']} · {source_count}{quiet} · {_times_label(cat['digest_time'])}")


_STATUS_ICON = {"pending": "⏳", "error": "⚠️"}


def _source_label(source) -> str:
    icon = _STATUS_ICON.get(source["status"]) or ("📡" if source["type"] == "telegram" else "🔗")
    return _clip(f"{icon} {source['name']}")


def _filter_label(rule: str, scope_count: int, hits: int) -> str:
    """Scope is what makes a rule safe or destructive, and the hit count is what
    separates a dead rule from the one to audit — both belong on the row."""
    scope = f"{scope_count} cat" if scope_count else "all"
    room = _LABEL_MAX - len(f" · {scope} · {hits}")
    return f"{_clip(rule, room)} · {scope} · {hits}"


def _is_rss(url: str) -> bool:
    return url.startswith("http://") or url.startswith("https://")


def _back_kb(back_data: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("« Back", callback_data=back_data)]])


async def _categories_keyboard(cats, page: int = 0, reorder: bool = False) -> InlineKeyboardMarkup:
    """Rows are full-width by default and grow ↑/↓ columns only in reorder mode:
    opening a category is the constant action, resorting one a rare one, and the
    arrows used to cost two thirds of every row (blank placeholders included)."""
    all_sources = await get_active_sources()
    pending = await get_pending_sources()
    errored = await get_error_sources()
    src_count: dict[str, int] = {}
    for s in list(all_sources) + list(pending) + list(errored):
        src_count[s["category"]] = src_count.get(s["category"], 0) + 1
    quiet_count: dict[str, int] = {}
    for row in await get_silent_sources(_QUIET_THRESHOLD_HOURS):
        quiet_count[row["category"]] = quiet_count.get(row["category"], 0) + 1

    total = len(cats)
    start = page * _PAGE_SIZE_CATS
    page_cats = cats[start:start + _PAGE_SIZE_CATS]
    total_pages = max(1, (total + _PAGE_SIZE_CATS - 1) // _PAGE_SIZE_CATS)

    buttons = []
    for i, r in enumerate(page_cats):
        global_idx = start + i
        label = _category_label(r, src_count.get(r["name"], 0), quiet_count.get(r["name"], 0))
        if not reorder:
            buttons.append([InlineKeyboardButton(label, callback_data=f"cat_view:{r['name']}")])
            continue
        buttons.append([
            InlineKeyboardButton("↑" if global_idx > 0 else " ", callback_data=f"cat_order_up:{page}:{r['name']}" if global_idx > 0 else "noop"),
            InlineKeyboardButton(label, callback_data="noop"),
            InlineKeyboardButton("↓" if global_idx < total - 1 else " ", callback_data=f"cat_order_down:{page}:{r['name']}" if global_idx < total - 1 else "noop"),
        ])

    if total_pages > 1:
        nav = []
        base = "cat_reorder" if reorder else "cat_list"
        if page > 0:
            nav.append(InlineKeyboardButton("‹", callback_data=f"{base}:{page - 1}"))
        nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton("›", callback_data=f"{base}:{page + 1}"))
        buttons.append(nav)

    if reorder:
        buttons.append([InlineKeyboardButton("✅ Done", callback_data=f"cat_list:{page}")])
    else:
        buttons.append([
            InlineKeyboardButton("➕ Add", callback_data="cat_add"),
            InlineKeyboardButton("⇅ Reorder", callback_data=f"cat_reorder:{page}"),
            InlineKeyboardButton("🕐 Timetable", callback_data="tt_list"),
        ])
        buttons.append([InlineKeyboardButton("« Home", callback_data="home")])
    return InlineKeyboardMarkup(buttons)


def _category_view_keyboard(cat_name: str, sources, page: int = 0, reorder: bool = False) -> InlineKeyboardMarkup:
    total = len(sources)
    start = page * _PAGE_SIZE_SOURCES
    page_sources = sources[start:start + _PAGE_SIZE_SOURCES]
    total_pages = max(1, (total + _PAGE_SIZE_SOURCES - 1) // _PAGE_SIZE_SOURCES)

    buttons = []
    for i, s in enumerate(page_sources):
        global_idx = start + i
        if not reorder:
            buttons.append([InlineKeyboardButton(_source_label(s), callback_data=f"src_view:{s['id']}")])
            continue
        buttons.append([
            InlineKeyboardButton("↑" if global_idx > 0 else " ", callback_data=f"src_order_up:{s['id']}:{page}:{cat_name}" if global_idx > 0 else "noop"),
            InlineKeyboardButton(_source_label(s), callback_data="noop"),
            InlineKeyboardButton("↓" if global_idx < total - 1 else " ", callback_data=f"src_order_down:{s['id']}:{page}:{cat_name}" if global_idx < total - 1 else "noop"),
        ])

    if total_pages > 1:
        nav = []
        base = "src_reorder" if reorder else "cat_view"
        if page > 0:
            nav.append(InlineKeyboardButton("‹", callback_data=f"{base}:{cat_name}:{page - 1}"))
        nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton("›", callback_data=f"{base}:{cat_name}:{page + 1}"))
        buttons.append(nav)

    if reorder:
        buttons.append([InlineKeyboardButton("✅ Done", callback_data=f"cat_view:{cat_name}:{page}")])
        return InlineKeyboardMarkup(buttons)
    buttons.append([
        InlineKeyboardButton("➕ Add source", callback_data=f"src_add:{cat_name}"),
        InlineKeyboardButton("⇅ Reorder", callback_data=f"src_reorder:{cat_name}:{page}"),
        InlineKeyboardButton("✏️ Edit", callback_data=f"cat_edit:{cat_name}"),
    ])
    buttons.append([
        InlineKeyboardButton("📝 Bulk prompt", callback_data=f"cat_bulk_prompt:{cat_name}"),
        InlineKeyboardButton("🗑 Delete", callback_data=f"cat_del:{cat_name}"),
    ])
    buttons.append([InlineKeyboardButton("« Categories", callback_data="cat_list")])
    return InlineKeyboardMarkup(buttons)


def _cat_edit_keyboard(cat_name: str) -> InlineKeyboardMarkup:
    # Digest time is edited in the timetable, where the whole day is visible at once.
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✏️ Rename", callback_data=f"cat_edit_field:{cat_name}:name")],
        [InlineKeyboardButton("🎨 Change emoji", callback_data=f"cat_edit_field:{cat_name}:emoji")],
        [InlineKeyboardButton("🕐 Timetable", callback_data="tt_list")],
        [InlineKeyboardButton("« Back", callback_data=f"cat_view:{cat_name}")],
    ])


def _source_view_keyboard(source_id: int, cat_name: str, url: str | None = None) -> InlineKeyboardMarkup:
    """The first row is the one that gets used: open the source, or edit how it is
    summarised. The rest are rare verbs and share a row."""
    top = [InlineKeyboardButton("📝 Prompt", callback_data=f"src_prompt:{source_id}")]
    if url:
        top.insert(0, InlineKeyboardButton("↗ Open", url=url))
    return InlineKeyboardMarkup([
        top,
        [
            InlineKeyboardButton("✏️ Rename", callback_data=f"src_rename:{source_id}"),
            InlineKeyboardButton("🔄 Move", callback_data=f"src_reassign:{source_id}"),
            InlineKeyboardButton("🗑 Remove", callback_data=f"src_del:{source_id}"),
        ],
        [InlineKeyboardButton("« Back", callback_data=f"cat_view:{cat_name}")],
    ])


def _source_view_text(source, health: dict) -> str:
    """The source screen answers one question — is this thing still worth keeping —
    so freshness and weekly volume lead, and the machinery (url, type) does not."""
    pending = source["status"] == "pending"
    icon = "⏳" if pending else ("📡" if source["type"] == "telegram" else "🔗")
    kind = "tg" if source["type"] == "telegram" else "rss"
    lines = [f"<b>{icon} {escape(source['name'])}</b>  <i>{kind} · {escape(source['category'])}</i>", ""]

    if pending:
        lines.append("<i>⏳ Pending — not joined yet, nothing collected.</i>")
    else:
        hours = health["hours_since"]
        quiet = hours is not None and hours >= _QUIET_THRESHOLD_HOURS
        freshness = "⏸ silent " + ago(hours) if quiet or hours is None else "Last item <b>" + ago(hours) + "</b>"
        total = health["week_total"]
        line = f"{freshness} · <b>{total}</b> in 7d"
        if health["week_muted"]:
            line += f" · {health['week_muted']} muted as dupes"
        lines.append(line)

    if source["prompt_extra"]:
        lines.append(f"Prompt: <i>{escape(source['prompt_extra'])}</i>")
    if not source_link(source["type"], source["url"]):
        # Nothing to link to, so the raw handle is the only way to identify it.
        lines.append(f"<code>{escape(source['url'])}</code>")
    return "\n".join(lines)


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
    buttons.append([InlineKeyboardButton("« Back", callback_data="cat_list")])
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
    buttons.append([InlineKeyboardButton("« Back", callback_data="tt_list")])
    return InlineKeyboardMarkup(buttons)


def _add_time_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="tt_list")]])


async def _blocked_keyboard(words, page: int = 0) -> InlineKeyboardMarkup:
    scopes = await get_word_category_map()
    hits = await get_blocked_hit_counts()
    total = len(words)
    start = page * _PAGE_SIZE_BLOCKED
    page_words = words[start:start + _PAGE_SIZE_BLOCKED]
    total_pages = max(1, (total + _PAGE_SIZE_BLOCKED - 1) // _PAGE_SIZE_BLOCKED)

    buttons = []
    for w in page_words:
        label = _filter_label(w["rule"], len(scopes.get(w["id"], ())), hits.get(w["rule"], 0))
        buttons.append([InlineKeyboardButton(label, callback_data=f"blocked_view:{w['id']}")])

    if total_pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("‹", callback_data=f"blocked_list:{page - 1}"))
        nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))
        if page < total_pages - 1:
            nav.append(InlineKeyboardButton("›", callback_data=f"blocked_list:{page + 1}"))
        buttons.append(nav)

    buttons.append([
        InlineKeyboardButton("➕ Add filter", callback_data="blocked_add"),
        InlineKeyboardButton("« Home", callback_data="home"),
    ])
    return InlineKeyboardMarkup(buttons)


async def render_categories(cats) -> str:
    """The list title with live counts, so every entry point renders the same screen."""
    sources = (list(await get_active_sources()) + list(await get_pending_sources())
               + list(await get_error_sources()))
    quiet = len(await get_silent_sources(_QUIET_THRESHOLD_HOURS))
    return _categories_text(cats, len(sources), quiet)


def _categories_text(cats, source_count: int, quiet_count: int = 0) -> str:
    """Title line for the category list. The counts are here rather than only on
    the rows so the screen still says something when the list is empty."""
    if not cats:
        return (
            "<b>📚 Categories</b>\n\nNothing set up yet.\n\n"
            "<i>➕ adds a category — a name and an emoji; sources go inside it.</i>"
        )
    quiet = f" · ⏸ {quiet_count} quiet" if quiet_count else ""
    return (
        f"<b>📚 Categories</b> · {len(cats)} · {source_count} source"
        f"{'' if source_count == 1 else 's'}{quiet}\n\n"
        "<i>Tap a category to open its sources.</i>"
    )


def _blocked_text(words, hits_total: int = 0) -> str:
    if not words:
        return (
            "<b>🚫 Filters</b>\n\nNo rules yet.\n\n"
            "<i>➕ adds one — describe what to drop, e.g. «crypto price predictions».</i>"
        )
    hits = f" · {hits_total} blocks in 30d" if hits_total else ""
    return (
        f"<b>🚫 Filters</b> · {len(words)}{hits}\n\n"
        "<i>Tap a rule to scope it to categories · rule · scope · 30d hits.</i>"
    )


async def _cat_view_text(cat_name: str) -> tuple[str, list]:
    cats = await get_categories()
    active = [s for s in await get_active_sources() if s["category"] == cat_name]
    # Errored sources belong on this screen more than anywhere else: it is where
    # they get fixed or removed.
    pending = list(await get_pending_sources(cat_name)) + list(await get_error_sources(cat_name))
    cat = next((c for c in cats if c["name"] == cat_name), None)
    emoji = cat["emoji"] if cat else "📌"
    digest_time = cat["digest_time"] if cat else _DEFAULT_DIGEST_TIME
    all_sources = sorted(active + pending, key=lambda s: (s["sort_order"], s["name"]))
    count = len(all_sources)
    if not all_sources:
        text = (
            f"<b>{emoji} {cat_name}</b>  ·  ⏰ {digest_time}\n\nNo sources yet.\n\n"
            "<i>➕ adds one — a @channel, a t.me link or an RSS url.</i>"
        )
    else:
        label = "source" if count == 1 else "sources"
        text = f"<b>{emoji} {cat_name}</b>  ·  ⏰ {digest_time}  ·  {count} {label}"
    return text, all_sources

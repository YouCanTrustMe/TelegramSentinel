import logging
from html import escape

from pyrogram import filters as pf
from pyrogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from src.bot.keyboards import _back_kb, _blocked_keyboard, _blocked_text
from src.bot.state import _pending
from src.db.models import (
    get_blocked_hit_counts,
    get_blocked_words,
    get_all_sources,
    get_categories,
    get_categories_for_word,
    get_source,
    get_sources_for_word,
    link_word_category,
    remove_blocked_word,
    toggle_word_source,
    unlink_word_category,
)

log = logging.getLogger(__name__)

# Room for a block title in the digest's Filtered section; the cut rule text it
# replaces runs to 45.
RULE_LABEL_MAX = 40


SOURCES_PAGE = 10


def rule_source_choices(sources, scoped_categories: set[str], chosen=()) -> list:
    """The sources a rule can be limited to: those of its categories, or all of them for a
    rule that applies everywhere, ordered as the source lists are. A source already chosen
    stays on the list even when it left those categories or was removed, or a limit nobody
    can see would keep the rule from ever firing."""
    pool = [s for s in sources if not scoped_categories or s["category"] in scoped_categories]
    listed = {s["id"] for s in pool}
    pool += [s for s in chosen if s["id"] not in listed]
    return sorted(pool, key=lambda s: ((s["category"] or ""), s["name"].lower()))


def parse_rule_label(text: str) -> str | None:
    """A short name as typed: "-" clears it, so the block falls back to the cut rule text."""
    label = " ".join(text.split())
    return None if label in ("", "-") else label[:RULE_LABEL_MAX]

async def _render_blocked_view(word_id: int) -> tuple[str, InlineKeyboardMarkup] | None:
    words = await get_blocked_words()
    word = next((w for w in words if w["id"] == word_id), None)
    if not word:
        return None
    categories = await get_categories()
    scoped = set(await get_categories_for_word(word_id))
    limited = await get_sources_for_word(word_id)

    buttons, row = [], []
    for cat in categories:
        name = cat["name"]
        mark = "✅" if name in scoped else "⬜"
        # Embed the stable category id (not the name) to keep callback_data short and rename-safe.
        row.append(InlineKeyboardButton(f"{mark} {name}", callback_data=f"blocked_cat_toggle:{word_id}:{cat['id']}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton(
        f"📡 Sources: {len(limited)} selected" if limited else "📡 Sources: all", callback_data=f"blocked_src:{word_id}:0")])
    buttons.append([
        InlineKeyboardButton("🏷 Short name", callback_data=f"blocked_label:{word_id}"),
        InlineKeyboardButton("🗑 Remove", callback_data=f"blocked_del:{word_id}"),
    ])
    buttons.append([InlineKeyboardButton("« Back", callback_data="blocked_list")])

    applies = f"selected ({len(scoped)})" if scoped else "all categories"
    by_id = {row["id"]: row for row in await get_all_sources()}
    stray = [by_id[sid]["name"] for sid in limited if sid in by_id and scoped and by_id[sid]["category"] not in scoped]
    warn = (f"\n⚠️ Outside these categories, so never judged: {escape(', '.join(stray))}" if stray else "")
    if any(sid not in by_id for sid in limited):
        warn += "\n⚠️ Limited to a removed source, so never judged there"
    short = word["label"] or "—"
    text = (
        f"🔴 {escape(word['rule'])}\n\n"
        f"<b>Short name:</b> {escape(short)}\n"
        f"<b>Applies to:</b> {applies}\n"
        f"<b>Sources:</b> {f'{len(limited)} selected' if limited else 'all of these categories'}{warn}\n"
        f"<i>Tap a category to toggle this filter. None selected = applies everywhere.</i>"
    )
    return text, InlineKeyboardMarkup(buttons)


async def _render_rule_sources(word_id: int, page: int) -> tuple[str, InlineKeyboardMarkup] | None:
    if not any(w["id"] == word_id for w in await get_blocked_words()):
        return None
    limited = set(await get_sources_for_word(word_id))
    sources = await get_all_sources()
    by_id = {row["id"]: row for row in sources}
    # A removed source stays listed while the rule is limited to it, so the limit can be taken off.
    chosen = [by_id.get(sid) or {"id": sid, "name": f"removed source #{sid}", "category": ""} for sid in sorted(limited)]
    choices = rule_source_choices(sources, set(await get_categories_for_word(word_id)), chosen)
    pages = max(1, (len(choices) + SOURCES_PAGE - 1) // SOURCES_PAGE)
    page = min(max(page, 0), pages - 1)
    buttons = [
        [InlineKeyboardButton(f"{'✅' if s['id'] in limited else '⬜'} {s['name']}" + (f" · {s['category']}" if s['category'] else ""),
                              callback_data=f"blocked_src_toggle:{word_id}:{s['id']}:{page}")]
        for s in choices[page * SOURCES_PAGE:(page + 1) * SOURCES_PAGE]
    ]
    if pages > 1:
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("‹", callback_data=f"blocked_src:{word_id}:{page - 1}"))
        nav.append(InlineKeyboardButton(f"{page + 1}/{pages}", callback_data="noop"))
        if page < pages - 1:
            nav.append(InlineKeyboardButton("›", callback_data=f"blocked_src:{word_id}:{page + 1}"))
        buttons.append(nav)
    buttons.append([InlineKeyboardButton("« Back", callback_data=f"blocked_view:{word_id}")])
    text = ("<b>Sources this rule judges</b>\n"
            "<i>Tap to toggle. None selected = every source of the rule's categories.</i>")
    return text, InlineKeyboardMarkup(buttons)


async def _hit_total(words) -> int:
    """Only rules that still exist count: blocked_reason rows outlive a deleted
    rule, and the header read "40 blocks" over a list where every rule showed 0."""
    hits = await get_blocked_hit_counts()
    return sum(hits.get(w["rule"], 0) for w in words)


def register_blocked_handlers(bot, admin_msg, admin_cb) -> None:

    @bot.on_message(pf.command("blocked") & admin_msg)
    async def cmd_blocked(_, message: Message) -> None:
        words = await get_blocked_words()
        await message.reply(
            _blocked_text(words, await _hit_total(words)),
            reply_markup=await _blocked_keyboard(words),
        )

    @bot.on_callback_query(pf.regex(r"^blocked_list(:\d+)?$") & admin_cb)
    async def cb_blocked_list(_, query: CallbackQuery) -> None:
        _pending.pop(query.from_user.id, None)
        parts = query.data.split(":")
        page = int(parts[1]) if len(parts) > 1 else 0
        words = await get_blocked_words()
        await query.message.edit_text(
            _blocked_text(words, await _hit_total(words)),
            reply_markup=await _blocked_keyboard(words, page),
        )

    @bot.on_callback_query(pf.regex(r"^blocked_add$") & admin_cb)
    async def cb_blocked_add(_, query: CallbackQuery) -> None:
        uid = query.from_user.id
        _pending[uid] = {"action": "add_blocked_word", "step": 0, "data": {}}
        await query.message.edit_text(
            "Enter a filter rule description (e.g. 'space launches and commercial rockets').\n"
            "Start with <code>=</code> to match the text literally instead of asking the model — "
            "the rest of the line is looked for anywhere in the post, so keep it specific "
            "(e.g. <code>= all clear</code>). Case, dash and apostrophe variants count as the same, "
            "so <code>= 📣 - реклама</code> also catches 📣– реклама; a rule starting or ending "
            "with a dash also matches hyphenated words:",
            reply_markup=_back_kb("blocked_list"),
        )

    @bot.on_callback_query(pf.regex(r"^blocked_view:") & admin_cb)
    async def cb_blocked_view(_, query: CallbackQuery) -> None:
        word_id = int(query.data.split(":", 1)[1])
        rendered = await _render_blocked_view(word_id)
        if rendered is None:
            await query.answer("Filter rule not found.", show_alert=True)
            return
        text, kb = rendered
        await query.message.edit_text(text, reply_markup=kb)

    @bot.on_callback_query(pf.regex(r"^blocked_cat_toggle:\d+:\d+$") & admin_cb)
    async def cb_blocked_cat_toggle(_, query: CallbackQuery) -> None:
        _, word_id_s, cat_id_s = query.data.split(":")
        word_id, cat_id = int(word_id_s), int(cat_id_s)
        category = next((c["name"] for c in await get_categories() if c["id"] == cat_id), None)
        if category is None:
            await query.answer("Category not found.", show_alert=True)
            return
        if category in set(await get_categories_for_word(word_id)):
            await unlink_word_category(word_id, category)
            log.info("Filter rule id=%d unscoped from category=%s", word_id, category)
        else:
            await link_word_category(word_id, category)
            log.info("Filter rule id=%d scoped to category=%s", word_id, category)
        rendered = await _render_blocked_view(word_id)
        if rendered is None:
            await query.answer("Filter rule not found.", show_alert=True)
            return
        text, kb = rendered
        await query.message.edit_text(text, reply_markup=kb)

    @bot.on_callback_query(pf.regex(r"^blocked_src:\d+:\d+$") & admin_cb)
    async def cb_blocked_sources(_, query: CallbackQuery) -> None:
        _, word_id_s, page_s = query.data.split(":")
        rendered = await _render_rule_sources(int(word_id_s), int(page_s))
        if rendered is None:
            await query.answer("Filter rule not found.", show_alert=True)
            return
        text, kb = rendered
        await query.message.edit_text(text, reply_markup=kb)

    @bot.on_callback_query(pf.regex(r"^blocked_src_toggle:\d+:\d+:\d+$") & admin_cb)
    async def cb_blocked_source_toggle(_, query: CallbackQuery) -> None:
        _, word_id_s, source_id_s, page_s = query.data.split(":")
        word_id, source_id = int(word_id_s), int(source_id_s)
        if not any(w["id"] == word_id for w in await get_blocked_words()):
            await query.answer("Filter rule not found.", show_alert=True)
            return
        if await get_source(source_id) is None and source_id not in await get_sources_for_word(word_id):
            await query.answer("Source not found.", show_alert=True)
            return
        on = await toggle_word_source(word_id, source_id)
        log.info("Filter rule id=%d %s source id=%d", word_id, "limited to" if on else "no longer limited to", source_id)
        rendered = await _render_rule_sources(word_id, int(page_s))
        if rendered is None:
            await query.answer("Filter rule not found.", show_alert=True)
            return
        text, kb = rendered
        await query.message.edit_text(text, reply_markup=kb)

    @bot.on_callback_query(pf.regex(r"^blocked_label:\d+$") & admin_cb)
    async def cb_blocked_label(_, query: CallbackQuery) -> None:
        word_id = int(query.data.split(":", 1)[1])
        _pending[query.from_user.id] = {"action": "label_blocked_word", "step": 0, "data": {"word_id": word_id}}
        await query.message.edit_text(
            f"Send a short name for this rule (up to {RULE_LABEL_MAX} characters), e.g. "
            "<code>crypto noise</code>. It titles the rule's block under Filtered in the digest "
            "and its row here. Send <code>-</code> to drop it.",
            reply_markup=_back_kb(f"blocked_view:{word_id}"),
        )

    @bot.on_callback_query(pf.regex(r"^blocked_del:") & admin_cb)
    async def cb_blocked_del(_, query: CallbackQuery) -> None:
        word_id = int(query.data.split(":", 1)[1])
        removed = await remove_blocked_word(word_id)
        if removed:
            log.info("Filter rule removed: id=%s", word_id)
        words = await get_blocked_words()
        await query.message.edit_text(
            _blocked_text(words, await _hit_total(words)),
            reply_markup=await _blocked_keyboard(words),
        )

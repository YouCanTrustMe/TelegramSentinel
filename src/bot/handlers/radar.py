import logging
from datetime import datetime, timezone
from html import escape

from pyrogram import filters as pf
from pyrogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from src.bot.keyboards import _back_kb, _confirm_keyboard
from src.bot.state import _pending
from src.collectors.folder_manager import RADAR_FOLDER, add_to_folder, remove_from_folder
from src.collectors.telegram_collector import userbot
from src.db.models import (
    add_radar_blacklist,
    add_radar_chat,
    add_radar_keyword,
    get_chats_for_keyword,
    get_keyword_chat_links,
    get_keyword_ids_for_chat,
    get_radar_blacklist,
    get_radar_chats,
    get_radar_keywords,
    get_recent_radar_alerts,
    link_keyword_chat,
    remove_radar_blacklist,
    remove_radar_chat,
    remove_radar_keyword,
    unlink_keyword_chat,
)

log = logging.getLogger(__name__)

_PAGE_SIZE = 10
_start_time = datetime.now(timezone.utc)


def _radar_main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📋 Keywords", callback_data="radar_keywords:0"),
            InlineKeyboardButton("💬 Chats", callback_data="radar_chats:0"),
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
        suffix = f" — ⚠️ unbound" if kw_count == 0 else f" · {kw_count}kw"
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


def register_radar_bot_handlers(bot, admin_msg, admin_cb) -> None:

    @bot.on_message(pf.command("radar") & admin_msg)
    async def cmd_radar(_, message: Message) -> None:
        await message.reply(
            "🔍 <b>Radar</b> — real-time keyword alerts",
            reply_markup=_radar_main_kb(),
        )

    @bot.on_callback_query(pf.regex(r"^radar_main$") & admin_cb)
    async def cb_radar_main(_, query: CallbackQuery) -> None:
        _pending.pop(query.from_user.id, None)
        await query.message.edit_text(
            "🔍 <b>Radar</b> — real-time keyword alerts",
            reply_markup=_radar_main_kb(),
        )

    # --- Keywords ---

    @bot.on_callback_query(pf.regex(r"^radar_keywords(:\d+)?$") & admin_cb)
    async def cb_radar_keywords(_, query: CallbackQuery) -> None:
        _pending.pop(query.from_user.id, None)
        parts = query.data.split(":")
        page = int(parts[1]) if len(parts) > 1 else 0
        text, kb = await _render_keywords(page)
        await query.message.edit_text(text, reply_markup=kb)

    @bot.on_callback_query(pf.regex(r"^radar_kw_view:\d+$") & admin_cb)
    async def cb_radar_kw_view(_, query: CallbackQuery) -> None:
        kw_id = int(query.data.split(":")[1])
        all_kw = await get_radar_keywords()
        kw_row = next((k for k in all_kw if k["id"] == kw_id), None)
        if not kw_row:
            await query.answer("Keyword not found.", show_alert=True)
            return
        linked_chats = await get_chats_for_keyword(kw_id)
        if linked_chats:
            chat_lines = "\n".join(f"• {escape(_chat_label(c))}" for c in linked_chats)
            text = (
                f"📋 <b>{escape(kw_row['keyword'])}</b>\n\n"
                f"Linked to <b>{len(linked_chats)}</b> chat(s):\n{chat_lines}\n\n"
                f"<i>Edit links from the chat side: Chats → tap chat → toggle keywords.</i>"
            )
        else:
            text = (
                f"📋 <b>{escape(kw_row['keyword'])}</b>\n\n"
                f"⚠️ Not linked to any chat yet.\n\n"
                f"<i>Open Chats → tap a chat → toggle this keyword on.</i>"
            )
        await query.message.edit_text(text, reply_markup=_back_kb("radar_keywords:0"))

    @bot.on_callback_query(pf.regex(r"^radar_kw_add$") & admin_cb)
    async def cb_radar_kw_add(_, query: CallbackQuery) -> None:
        uid = query.from_user.id
        _pending[uid] = {"action": "add_radar_keyword", "step": 0, "data": {}}
        await query.message.edit_text(
            "Send keyword to add:",
            reply_markup=_back_kb("radar_keywords:0"),
        )

    @bot.on_callback_query(pf.regex(r"^radar_kw_del:\d+$") & admin_cb)
    async def cb_radar_kw_del(_, query: CallbackQuery) -> None:
        kw_id = int(query.data.split(":")[1])
        items = await get_radar_keywords()
        kw_row = next((k for k in items if k["id"] == kw_id), None)
        label = kw_row["keyword"] if kw_row else str(kw_id)
        await query.message.edit_text(
            f"Remove keyword <b>{escape(label)}</b>?",
            reply_markup=_confirm_keyboard(f"radar_kw_del_ok:{kw_id}", "radar_keywords:0"),
        )

    @bot.on_callback_query(pf.regex(r"^radar_kw_del_ok:\d+$") & admin_cb)
    async def cb_radar_kw_del_ok(_, query: CallbackQuery) -> None:
        kw_id = int(query.data.split(":")[1])
        await remove_radar_keyword(kw_id)
        log.info("Radar keyword removed: id=%d", kw_id)
        text, kb = await _render_keywords(0)
        await query.message.edit_text(text, reply_markup=kb)

    # --- Chats ---

    @bot.on_callback_query(pf.regex(r"^radar_chats(:\d+)?$") & admin_cb)
    async def cb_radar_chats(_, query: CallbackQuery) -> None:
        _pending.pop(query.from_user.id, None)
        parts = query.data.split(":")
        page = int(parts[1]) if len(parts) > 1 else 0
        text, kb = await _render_chats(page)
        await query.message.edit_text(text, reply_markup=kb)

    async def _render_chat_edit(chat_id: int, page: int) -> tuple[str, InlineKeyboardMarkup]:
        chats = await get_radar_chats()
        chat_row = next((c for c in chats if c["id"] == chat_id), None)
        if not chat_row:
            return "Chat not found.", _back_kb("radar_chats:0")
        keywords = await get_radar_keywords()
        linked = await get_keyword_ids_for_chat(chat_id)
        total = len(keywords)
        total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)
        page = max(0, min(page, total_pages - 1))
        start = page * _PAGE_SIZE
        page_items = keywords[start:start + _PAGE_SIZE]

        buttons = []
        for k in page_items:
            mark = "✅" if k["id"] in linked else "⬜"
            buttons.append([InlineKeyboardButton(
                f"{mark} {k['keyword']}",
                callback_data=f"radar_link_toggle:{chat_id}:{k['id']}:{page}",
            )])

        if total_pages > 1:
            nav = []
            if page > 0:
                nav.append(InlineKeyboardButton("◀", callback_data=f"radar_chat_view:{chat_id}:{page - 1}"))
            nav.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))
            if page < total_pages - 1:
                nav.append(InlineKeyboardButton("▶", callback_data=f"radar_chat_view:{chat_id}:{page + 1}"))
            buttons.append(nav)

        buttons.append([InlineKeyboardButton("◀ Back", callback_data="radar_chats:0")])

        status_line = f" · status: <b>{chat_row['status']}</b>" if chat_row["status"] != "active" else ""
        if keywords:
            text = (
                f"💬 <b>{escape(chat_row['title'] or chat_row['chat_ref'])}</b>"
                f"{status_line}\n\n"
                f"Tap a keyword to toggle monitoring for this chat.\n"
                f"Linked: <b>{len(linked)}</b>/{total}"
            )
        else:
            text = (
                f"💬 <b>{escape(chat_row['title'] or chat_row['chat_ref'])}</b>"
                f"{status_line}\n\n"
                f"⚠️ No keywords defined yet. Add some in Keywords first."
            )
        return text, InlineKeyboardMarkup(buttons)

    @bot.on_callback_query(pf.regex(r"^radar_chat_view:\d+(:\d+)?$") & admin_cb)
    async def cb_radar_chat_view(_, query: CallbackQuery) -> None:
        parts = query.data.split(":")
        chat_id = int(parts[1])
        page = int(parts[2]) if len(parts) > 2 else 0
        text, kb = await _render_chat_edit(chat_id, page)
        await query.message.edit_text(text, reply_markup=kb)

    @bot.on_callback_query(pf.regex(r"^radar_link_toggle:\d+:\d+:\d+$") & admin_cb)
    async def cb_radar_link_toggle(_, query: CallbackQuery) -> None:
        _, chat_id_s, kw_id_s, page_s = query.data.split(":")
        chat_id, kw_id, page = int(chat_id_s), int(kw_id_s), int(page_s)
        linked = await get_keyword_ids_for_chat(chat_id)
        if kw_id in linked:
            await unlink_keyword_chat(kw_id, chat_id)
            log.info("Radar link removed: kw_id=%d chat_id=%d", kw_id, chat_id)
        else:
            await link_keyword_chat(kw_id, chat_id)
            log.info("Radar link added: kw_id=%d chat_id=%d", kw_id, chat_id)
        text, kb = await _render_chat_edit(chat_id, page)
        await query.message.edit_text(text, reply_markup=kb)

    @bot.on_callback_query(pf.regex(r"^radar_chat_add$") & admin_cb)
    async def cb_radar_chat_add(_, query: CallbackQuery) -> None:
        uid = query.from_user.id
        _pending[uid] = {"action": "add_radar_chat", "step": 0, "data": {}}
        await query.message.edit_text(
            "Send chat_id (integer) or @username:",
            reply_markup=_back_kb("radar_chats:0"),
        )

    @bot.on_callback_query(pf.regex(r"^radar_chat_del:\d+$") & admin_cb)
    async def cb_radar_chat_del(_, query: CallbackQuery) -> None:
        entry_id = int(query.data.split(":")[1])
        all_chats = await get_radar_chats()
        chat_row = next((c for c in all_chats if c["id"] == entry_id), None)
        label = _chat_label(chat_row) if chat_row else str(entry_id)
        await query.message.edit_text(
            f"Remove monitored chat <b>{escape(label)}</b>?\n"
            f"<i>Userbot will leave the chat.</i>",
            reply_markup=_confirm_keyboard(f"radar_chat_del_ok:{entry_id}", "radar_chats:0"),
        )

    @bot.on_callback_query(pf.regex(r"^radar_chat_del_ok:\d+$") & admin_cb)
    async def cb_radar_chat_del_ok(_, query: CallbackQuery) -> None:
        entry_id = int(query.data.split(":")[1])
        all_chats = await get_radar_chats()
        chat_row = next((c for c in all_chats if c["id"] == entry_id), None)
        await remove_radar_chat(entry_id)
        log.info("Radar chat removed: id=%d", entry_id)
        if chat_row:
            ref = chat_row["chat_ref"]
            await remove_from_folder(ref, RADAR_FOLDER)
            try:
                await userbot.leave_chat(ref if ref.startswith("@") else int(ref))
                log.info("Radar: left chat %s", ref)
            except Exception as exc:
                log.warning("Radar: could not leave chat %s: %s", ref, exc)
        text, kb = await _render_chats(0)
        await query.message.edit_text(text, reply_markup=kb)

    # --- Blacklist ---

    @bot.on_callback_query(pf.regex(r"^radar_blacklist(:\d+)?$") & admin_cb)
    async def cb_radar_blacklist(_, query: CallbackQuery) -> None:
        _pending.pop(query.from_user.id, None)
        parts = query.data.split(":")
        page = int(parts[1]) if len(parts) > 1 else 0
        items = await get_radar_blacklist()
        text = (
            f"🚫 <b>Blacklist</b> ({len(items)})"
            if items
            else "🚫 <b>Blacklist</b>\n\nNo users blacklisted."
        )
        kb = _radar_list_kb(
            items, page, "id", "radar_bl_del:", "radar_bl_add",
            "radar_blacklist", lambda r: str(r["user_id"]),
        )
        await query.message.edit_text(text, reply_markup=kb)

    @bot.on_callback_query(pf.regex(r"^radar_bl_add$") & admin_cb)
    async def cb_radar_bl_add(_, query: CallbackQuery) -> None:
        uid = query.from_user.id
        _pending[uid] = {"action": "add_radar_blacklist", "step": 0, "data": {}}
        await query.message.edit_text(
            "Send user_id (integer) to blacklist:",
            reply_markup=_back_kb("radar_blacklist:0"),
        )

    @bot.on_callback_query(pf.regex(r"^radar_bl_del:\d+$") & admin_cb)
    async def cb_radar_bl_del(_, query: CallbackQuery) -> None:
        entry_id = int(query.data.split(":")[1])
        items = await get_radar_blacklist()
        row = next((b for b in items if b["id"] == entry_id), None)
        label = str(row["user_id"]) if row else str(entry_id)
        await query.message.edit_text(
            f"Unblacklist user <b>{escape(label)}</b>?",
            reply_markup=_confirm_keyboard(f"radar_bl_del_ok:{entry_id}", "radar_blacklist:0"),
        )

    @bot.on_callback_query(pf.regex(r"^radar_bl_del_ok:\d+$") & admin_cb)
    async def cb_radar_bl_del_ok(_, query: CallbackQuery) -> None:
        entry_id = int(query.data.split(":")[1])
        await remove_radar_blacklist(entry_id)
        log.info("Radar blacklist entry removed: id=%d", entry_id)
        items = await get_radar_blacklist()
        text = (
            f"🚫 <b>Blacklist</b> ({len(items)})"
            if items
            else "🚫 <b>Blacklist</b>\n\nNo users blacklisted."
        )
        await query.message.edit_text(
            text,
            reply_markup=_radar_list_kb(
                items, 0, "id", "radar_bl_del:", "radar_bl_add",
                "radar_blacklist", lambda r: str(r["user_id"]),
            ),
        )

    # --- Status ---

    @bot.on_callback_query(pf.regex(r"^radar_status$") & admin_cb)
    async def cb_radar_status(_, query: CallbackQuery) -> None:
        chats = await get_radar_chats()
        keywords = await get_radar_keywords()
        blacklist = await get_radar_blacklist()
        alerts = await get_recent_radar_alerts(3)

        delta = datetime.now(timezone.utc) - _start_time
        hours, rem = divmod(int(delta.total_seconds()), 3600)
        minutes = rem // 60

        alert_lines = ""
        if alerts:
            alert_lines = "\n\n<b>Last alerts:</b>\n" + "\n".join(
                f"• \"{r['keyword']}\" in {r['chat_ref']} — {r['alerted_at'][:16]}"
                for r in alerts
            )

        text = (
            f"📊 <b>Radar Status</b>\n\n"
            f"Chats monitored: <b>{len(chats)}</b>\n"
            f"Keywords active: <b>{len(keywords)}</b>\n"
            f"Blacklisted users: <b>{len(blacklist)}</b>\n"
            f"Uptime: <b>{hours}h {minutes}m</b>"
            f"{alert_lines}"
        )
        await query.message.edit_text(text, reply_markup=_back_kb("radar_main"))

    # --- Text input handler for add flows ---

    @bot.on_message(pf.private & admin_msg, group=1)
    async def handle_radar_conversation(_, message: Message) -> None:
        if not message.text or message.text.startswith("/"):
            return
        uid = message.from_user.id
        state = _pending.get(uid)
        if not state or not state.get("action", "").startswith("add_radar"):
            return

        text = message.text.strip()
        action = state["action"]

        if action == "add_radar_keyword":
            keyword = text.lower()
            added = await add_radar_keyword(keyword)
            del _pending[uid]
            items = await get_radar_keywords()
            if added:
                log.info("Radar keyword added: %s", keyword)
                header = (
                    f"✅ Added: <code>{escape(keyword)}</code>\n\n"
                    f"📋 <b>Keywords</b> ({len(items)})"
                    if items
                    else f"✅ Added: <code>{escape(keyword)}</code>\n\n📋 <b>Keywords</b>\n\nNo keywords yet."
                )
            else:
                header = f"⚠️ Already exists: <code>{escape(keyword)}</code>\n\n📋 <b>Keywords</b> ({len(items)})"
            await message.reply(
                header,
                reply_markup=_radar_list_kb(
                    items, 0, "id", "radar_kw_del:", "radar_kw_add",
                    "radar_keywords", lambda r: r["keyword"],
                ),
            )

        elif action == "add_radar_chat":
            raw = text.strip()
            if "t.me/" in raw:
                path = raw.split("t.me/")[1].split("?")[0].rstrip("/")
                raw = f"@{path}" if not path.startswith("+") else raw
            if not (raw.startswith("@") or raw.lstrip("-").isdigit()):
                await message.reply(
                    "Invalid input. Send @username, chat_id, or t.me/username link:",
                    reply_markup=_back_kb("radar_chats:0"),
                )
                return
            title = None
            resolved_id: int | None = None
            try:
                chat = await userbot.get_chat(raw if raw.startswith("@") else int(raw))
                title = chat.title or chat.first_name or raw
                resolved_id = chat.id
                ref = f"@{chat.username}" if chat.username else str(chat.id)
            except Exception as exc:
                ref = raw
                log.warning("Could not resolve radar chat %s: %s", raw, exc)
            added = await add_radar_chat(ref, title, chat_id=resolved_id)
            del _pending[uid]
            if added:
                try:
                    await userbot.join_chat(ref if ref.startswith("@") else int(ref))
                    log.info("Radar: joined chat %s", ref)
                except Exception as exc:
                    log.warning("Radar: could not join chat %s: %s", ref, exc)
                await add_to_folder(ref, RADAR_FOLDER)
            items = await get_radar_chats()
            if added:
                log.info("Radar chat added: ref=%s title=%s", ref, title)
                header = (
                    f"✅ Added: <code>{escape(ref)}</code>"
                    + (f" — {escape(title)}" if title else "")
                    + f"\n\n💬 <b>Monitored chats</b> ({len(items)})"
                )
            else:
                header = f"⚠️ Already monitored: <code>{escape(ref)}</code>"
            await message.reply(
                header,
                reply_markup=_radar_list_kb(
                    items, 0, "id", "radar_chat_del:", "radar_chat_add",
                    "radar_chats", _chat_label,
                ),
            )

        elif action == "add_radar_blacklist":
            if not text.lstrip("-").isdigit():
                await message.reply(
                    "Invalid input. Send an integer user_id:",
                    reply_markup=_back_kb("radar_blacklist:0"),
                )
                return
            user_id = int(text)
            added = await add_radar_blacklist(user_id)
            del _pending[uid]
            items = await get_radar_blacklist()
            if added:
                log.info("Radar blacklist entry added: user_id=%d", user_id)
                header = f"✅ Blacklisted: <code>{user_id}</code>\n\n🚫 <b>Blacklist</b> ({len(items)})"
            else:
                header = f"⚠️ Already blacklisted: <code>{user_id}</code>"
            await message.reply(
                header,
                reply_markup=_radar_list_kb(
                    items, 0, "id", "radar_bl_del:", "radar_bl_add",
                    "radar_blacklist", lambda r: str(r["user_id"]),
                ),
            )

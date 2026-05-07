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

from src.bot.keyboards import _back_kb
from src.bot.state import _pending
from src.collectors.telegram_collector import userbot
from src.db.models import (
    add_radar_blacklist,
    add_radar_chat,
    add_radar_keyword,
    get_radar_blacklist,
    get_radar_chats,
    get_radar_keywords,
    get_recent_radar_alerts,
    remove_radar_blacklist,
    remove_radar_chat,
    remove_radar_keyword,
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
) -> InlineKeyboardMarkup:
    total = len(items)
    start = page * _PAGE_SIZE
    page_items = items[start : start + _PAGE_SIZE]
    total_pages = max(1, (total + _PAGE_SIZE - 1) // _PAGE_SIZE)

    buttons = []
    for row in page_items:
        buttons.append([
            InlineKeyboardButton(label_fn(row), callback_data="noop"),
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


def _chat_label(row) -> str:
    return f"{row['title']} ({row['chat_ref']})" if row["title"] else row["chat_ref"]


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
        items = await get_radar_keywords()
        text = (
            f"📋 <b>Keywords</b> ({len(items)})"
            if items
            else "📋 <b>Keywords</b>\n\nNo keywords yet."
        )
        kb = _radar_list_kb(
            items, page, "id", "radar_kw_del:", "radar_kw_add",
            "radar_keywords", lambda r: r["keyword"],
        )
        await query.message.edit_text(text, reply_markup=kb)

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
        await remove_radar_keyword(kw_id)
        log.info("Radar keyword removed: id=%d", kw_id)
        items = await get_radar_keywords()
        text = (
            f"📋 <b>Keywords</b> ({len(items)})"
            if items
            else "📋 <b>Keywords</b>\n\nNo keywords yet."
        )
        await query.message.edit_text(
            text,
            reply_markup=_radar_list_kb(
                items, 0, "id", "radar_kw_del:", "radar_kw_add",
                "radar_keywords", lambda r: r["keyword"],
            ),
        )

    # --- Chats ---

    @bot.on_callback_query(pf.regex(r"^radar_chats(:\d+)?$") & admin_cb)
    async def cb_radar_chats(_, query: CallbackQuery) -> None:
        _pending.pop(query.from_user.id, None)
        parts = query.data.split(":")
        page = int(parts[1]) if len(parts) > 1 else 0
        items = await get_radar_chats()
        text = (
            f"💬 <b>Monitored chats</b> ({len(items)})"
            if items
            else "💬 <b>Monitored chats</b>\n\nNo chats yet."
        )
        kb = _radar_list_kb(
            items, page, "id", "radar_chat_del:", "radar_chat_add",
            "radar_chats", _chat_label,
        )
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
        await remove_radar_chat(entry_id)
        log.info("Radar chat removed: id=%d", entry_id)
        items = await get_radar_chats()
        text = (
            f"💬 <b>Monitored chats</b> ({len(items)})"
            if items
            else "💬 <b>Monitored chats</b>\n\nNo chats yet."
        )
        await query.message.edit_text(
            text,
            reply_markup=_radar_list_kb(
                items, 0, "id", "radar_chat_del:", "radar_chat_add",
                "radar_chats", _chat_label,
            ),
        )

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
            try:
                chat = await userbot.get_chat(raw if raw.startswith("@") else int(raw))
                title = chat.title or chat.first_name or raw
                ref = f"@{chat.username}" if chat.username else str(chat.id)
            except Exception as exc:
                ref = raw
                log.warning("Could not resolve radar chat %s: %s", raw, exc)
            added = await add_radar_chat(ref, title)
            del _pending[uid]
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

import asyncio
import logging
from html import escape

import feedparser
from pyrogram import filters as pf
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from src.bot.handlers.categories import _finalize_add_category
from src.bot.handlers.sources import _finalize_add_source
from src.bot.keyboards import (
    _add_time_kb,
    _back_kb,
    _blocked_keyboard,
    _cat_view_text,
    _category_view_keyboard,
    _is_rss,
    _slot_keyboard,
    _slot_text,
    _source_view_keyboard,
)
from src.bot.state import _pending
from src.db.models import (
    add_blocked_word,
    bulk_set_category_prompt_extra,
    get_blocked_words,
    get_categories,
    get_source,
    get_sources_by_category,
    rename_source,
    set_source_prompt_extra,
    source_exists,
    update_category,
)
from src.collectors.rss_collector import _FEED_AGENT
from src.collectors.telegram_collector import userbot
from src.common.schedule import parse_times
from src.scheduler import rebuild_digest_jobs

log = logging.getLogger(__name__)


def register_conversation_handler(bot, admin_msg, admin_cb) -> None:

    @bot.on_message(pf.private & admin_msg)
    async def handle_conversation(_, message: Message) -> None:
        if not message.text or message.text.startswith("/"):
            return
        uid = message.from_user.id
        if uid not in _pending:
            return

        state = _pending[uid]
        text = message.text.strip()
        action = state["action"]
        step = state["step"]
        data = state["data"]

        if action == "add_category":
            if step == 0:
                data["name"] = text.lower()
                state["step"] = 1
                await message.reply("Emoji:", reply_markup=_back_kb("cat_list"))
            elif step == 1:
                data["emoji"] = escape(text[:8])
                await _finalize_add_category(uid, data, message)

        elif action == "add_digest_time":
            times = parse_times(text)
            if len(times) != 1:
                await message.reply(
                    "Send one time as HH:MM, e.g. 08:30:",
                    reply_markup=_add_time_kb(),
                )
                return
            del _pending[uid]
            h, m = times[0]
            time_str = f"{h:02d}:{m:02d}"
            cats = await get_categories()
            log.info("Timetable new time entered | time=%s", time_str)
            await message.reply(
                _slot_text(time_str, cats),
                reply_markup=_slot_keyboard(time_str, cats),
            )

        elif action == "add_blocked_word":
            added = await add_blocked_word(text)
            del _pending[uid]
            words = await get_blocked_words()
            if added:
                log.info("Filter rule added: %s", text)
                await message.reply(
                    f"✅ Added filter rule: <code>{escape(text)}</code>\n\n🚫 <b>Content filters</b>",
                    reply_markup=_blocked_keyboard(words),
                )
            else:
                await message.reply(
                    f"⚠️ Already exists: <code>{escape(text)}</code>",
                    reply_markup=_blocked_keyboard(words),
                )

        elif action == "add_source":
            if step == 0:
                url = text
                if "t.me/" in url:
                    path = url.split("t.me/")[1].split("?")[0].rstrip("/")
                    url = url if path.startswith("+") else f"@{path}"
                    source_type = "telegram"
                else:
                    source_type = "rss" if _is_rss(url) else "telegram"
                if await source_exists(url):
                    del _pending[uid]
                    await message.reply(f"Source <code>{url}</code> already exists.")
                    return
                if source_type == "telegram":
                    is_invite = "/+" in url or url.lstrip("@").startswith("+")
                    try:
                        chat = await userbot.get_chat(url.lstrip("@"))
                        name = chat.title
                    except Exception as exc:
                        if is_invite:
                            name = url
                        else:
                            del _pending[uid]
                            await message.reply(f"Could not fetch channel info: {exc}")
                            return
                else:
                    # Same UA as the collector — some feeds (Cloudflare-fronted) reject
                    # feedparser's default UA, so validating without it would wrongly
                    # reject a feed that the collector then fetches fine.
                    feed = await asyncio.to_thread(feedparser.parse, url, agent=_FEED_AGENT)
                    if feed.bozo and not feed.entries:
                        del _pending[uid]
                        await message.reply(f"❌ Not a valid RSS feed: <code>{escape(url)}</code>")
                        return
                    name = feed.feed.get("title") or url
                data["url"] = url
                data["name"] = name
                data["type"] = source_type

                if "preset_category" in data:
                    await _finalize_add_source(uid, data["preset_category"], data, message)
                    return

                state["step"] = 1
                cats = await get_categories()
                if cats:
                    keyboard = InlineKeyboardMarkup(
                        [[InlineKeyboardButton(f"{r['emoji']} {r['name']}", callback_data=f"add_src_cat:{r['name']}")] for r in cats]
                        + [[InlineKeyboardButton("◀ Back", callback_data="cat_list")]]
                    )
                    await message.reply(f"Name: <b>{name}</b>\nCategory:", reply_markup=keyboard)
                else:
                    await message.reply(f"Name: <b>{name}</b>\nCategory:", reply_markup=_back_kb("cat_list"))

            elif step == 1:
                await _finalize_add_source(uid, text, data, message)

        elif action == "rename_source":
            new_name = text.strip()
            src_id = data["source_id"]
            cat_name = data["cat_name"]
            ok = await rename_source(src_id, new_name)
            del _pending[uid]
            if ok:
                log.info("Source renamed: id=%s -> %s", src_id, new_name)
                s = await get_source(src_id)
                if s:
                    pending = s["status"] == "pending"
                    icon = "⏳" if pending else ("📡" if s["type"] == "telegram" else "🔗")
                    type_label = "tg" if s["type"] == "telegram" else "rss"
                    status_line = "\nStatus: <b>pending</b>" if pending else ""
                    src_text = (
                        f"{icon} <b>{escape(new_name)}</b>\n"
                        f"Type: <code>{type_label}</code>\n"
                        f"URL: <code>{s['url']}</code>\n"
                        f"Category: <b>{cat_name}</b>{status_line}"
                    )
                    await message.reply(
                        f"✅ Renamed to <b>{escape(new_name)}</b>.\n\n{src_text}",
                        reply_markup=_source_view_keyboard(src_id, cat_name),
                    )
                else:
                    await message.reply(f"✅ Renamed to <b>{escape(new_name)}</b>.")
            else:
                await message.reply("Source not found.")

        elif action == "edit_category":
            field = data["field"]
            cat_name = data["cat_name"]

            if field == "name":
                new_name = text.lower()
                ok = await update_category(cat_name, new_name=new_name)
                del _pending[uid]
                if ok:
                    await rebuild_digest_jobs()
                    log.info("Category renamed: %s -> %s", cat_name, new_name)
                    text_out, sources = await _cat_view_text(new_name)
                    await message.reply(
                        f"✅ Renamed to <b>{new_name}</b>.\n\n" + text_out,
                        reply_markup=_category_view_keyboard(new_name, sources),
                    )
                else:
                    await message.reply("Category not found.")

            elif field == "emoji":
                new_emoji = escape(text[:8])
                ok = await update_category(cat_name, new_emoji=new_emoji)
                del _pending[uid]
                if ok:
                    log.info("Category emoji updated: %s -> %s", cat_name, new_emoji)
                    text_out, sources = await _cat_view_text(cat_name)
                    await message.reply(
                        "✅ Emoji updated.\n\n" + text_out,
                        reply_markup=_category_view_keyboard(cat_name, sources),
                    )
                else:
                    await message.reply("Category not found.")

        elif action == "edit_source_prompt":
            src_id = state["data"]["source_id"]
            await set_source_prompt_extra(src_id, text)
            del _pending[uid]
            s = await get_source(src_id)
            log.info("Source prompt updated: id=%s", src_id)
            await message.reply(
                f"✅ Prompt set for <b>{escape(s['name']) if s else str(src_id)}</b>:\n<i>{escape(text)}</i>",
                reply_markup=_source_view_keyboard(src_id, s["category"] if s else ""),
            )

        elif action == "bulk_prompt_category":
            cat_name = state["data"]["cat_name"]
            value = None if text.lower() == "clear" else text
            count = await bulk_set_category_prompt_extra(cat_name, value)
            del _pending[uid]
            log.info("Bulk prompt set for category=%s count=%d", cat_name, count)
            sources = await get_sources_by_category(cat_name)
            if value:
                header = f"✅ Prompt set for <b>{count}</b> source(s) in <b>{cat_name}</b>:\n<i>{escape(value)}</i>"
            else:
                header = f"✅ Prompt cleared for <b>{count}</b> source(s) in <b>{cat_name}</b>."
            await message.reply(header, reply_markup=_category_view_keyboard(cat_name, sources))

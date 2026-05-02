import asyncio
import logging
from html import escape

import feedparser
from pyrogram import filters as pf
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from src.bot.handlers.categories import _finalize_add_category
from src.bot.handlers.sources import _finalize_add_source
from src.bot.keyboards import (
    _back_kb,
    _blocked_keyboard,
    _cancel_kb,
    _cat_view_text,
    _category_view_keyboard,
    _is_rss,
    _is_valid_time,
    _time_step_kb,
    _edit_time_kb,
)
from src.bot.state import _DEFAULT_DIGEST_TIME, _pending
from src.db.models import (
    add_blocked_word,
    get_blocked_words,
    get_categories,
    source_exists,
    update_category,
)
from src.collectors.telegram_collector import userbot
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
                await message.reply("Emoji:", reply_markup=_cancel_kb())
            elif step == 1:
                data["emoji"] = escape(text[:8])
                state["step"] = 2
                await message.reply(
                    f"Digest time (HH:MM) or skip for default ({_DEFAULT_DIGEST_TIME}):",
                    reply_markup=_time_step_kb(),
                )
            elif step == 2:
                if not _is_valid_time(text):
                    await message.reply(
                        "Invalid format. Use HH:MM (e.g. 16:00):",
                        reply_markup=_time_step_kb(),
                    )
                    return
                data["digest_time"] = text
                await _finalize_add_category(uid, data, message)

        elif action == "add_blocked_word":
            word = text.lower()
            added = await add_blocked_word(word)
            del _pending[uid]
            words = await get_blocked_words()
            if added:
                log.info("Blocked word added: %s", word)
                await message.reply(
                    f"✅ Added: <code>{escape(word)}</code>\n\n🚫 <b>Blocked words</b>",
                    reply_markup=_blocked_keyboard(words),
                )
            else:
                await message.reply(
                    f"⚠️ Already blocked: <code>{escape(word)}</code>",
                    reply_markup=_blocked_keyboard(words),
                )

        elif action == "add_source":
            if step == 0:
                url = text
                source_type = "rss" if _is_rss(url) else "telegram"
                if await source_exists(url):
                    del _pending[uid]
                    await message.reply(f"Source <code>{url}</code> already exists.")
                    return
                if source_type == "telegram":
                    try:
                        chat = await userbot.get_chat(url.lstrip("@"))
                        name = chat.title
                    except Exception as exc:
                        del _pending[uid]
                        await message.reply(f"Could not fetch channel info: {exc}")
                        return
                else:
                    feed = await asyncio.to_thread(feedparser.parse, url)
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
                        + [[InlineKeyboardButton("❌ Cancel", callback_data="cancel_flow")]]
                    )
                    await message.reply(f"Name: <b>{name}</b>\nCategory:", reply_markup=keyboard)
                else:
                    await message.reply(f"Name: <b>{name}</b>\nCategory:", reply_markup=_cancel_kb())

            elif step == 1:
                await _finalize_add_source(uid, text, data, message)

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
                        f"✅ Emoji updated.\n\n" + text_out,
                        reply_markup=_category_view_keyboard(cat_name, sources),
                    )
                else:
                    await message.reply("Category not found.")

            elif field == "time":
                if not _is_valid_time(text):
                    await message.reply(
                        "Invalid format. Use HH:MM (e.g. 16:00):",
                        reply_markup=_edit_time_kb(cat_name),
                    )
                    return
                ok = await update_category(cat_name, new_digest_time=text)
                del _pending[uid]
                if ok:
                    await rebuild_digest_jobs()
                    log.info("Category digest_time updated: %s -> %s", cat_name, text)
                    text_out, sources = await _cat_view_text(cat_name)
                    await message.reply(
                        f"✅ Digest time set to <b>{text}</b>.\n\n" + text_out,
                        reply_markup=_category_view_keyboard(cat_name, sources),
                    )
                else:
                    await message.reply("Category not found.")

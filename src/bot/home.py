"""The home screen: the state of the pipeline in six lines.

Rendering is split from collecting on purpose. `home_text` and the helpers above
it take plain values and return a string, so the screen the admin sees every day
is unit-testable without a database or a live bot.
"""
from datetime import datetime, timezone
from html import escape

from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from zoneinfo import ZoneInfo

from src.common.schedule import slots_by_time
from src.config import settings
from src.db.models import (
    count_unsent_items,
    get_active_sources,
    get_categories,
    get_blocked_words,
    get_error_sources,
    get_last_digest,
    get_pending_sources,
    get_paused_sources,
    get_silent_sources,
)

_QUIET_THRESHOLD_HOURS = 120


def _countdown(minutes: int) -> str:
    hours, mins = divmod(max(minutes, 0), 60)
    return f"{hours}h {mins:02d}m" if hours else f"{mins}m"


def next_fire(slots: list[str], now: datetime) -> tuple[str, int] | None:
    """Next digest slot and the minutes until it, wrapping past midnight to the
    first slot of tomorrow. None when nothing is scheduled at all."""
    if not slots:
        return None
    now_minutes = now.hour * 60 + now.minute
    parsed = sorted((int(t[:2]) * 60 + int(t[3:]), t) for t in slots)
    for at, label in parsed:
        if at > now_minutes:
            return label, at - now_minutes
    at, label = parsed[0]
    return label, at + 24 * 60 - now_minutes


def local_sent_at(sent_at: str, tz: ZoneInfo) -> datetime | None:
    """digest_log.sent_at is stored in UTC; every other time on this screen is in
    the digest timezone, so a Berlin 09:00 digest read back as "07:00" next to
    "Next digest 09:00" — and its issue number (the day of the year, see
    _digest_number) could land a day early."""
    try:
        sent = datetime.fromisoformat(sent_at)
    except (ValueError, TypeError):
        return None
    if sent.tzinfo is None:
        sent = sent.replace(tzinfo=timezone.utc)
    return sent.astimezone(tz)


def home_text(state: dict) -> str:
    """Render the home screen from a plain state dict — see gather_home_state."""
    quiet = state["quiet"]
    if not state["categories"]:
        status = "no categories yet"
    elif state["next"] is None:
        status = "⚠️ nothing scheduled"
    elif state["errored"]:
        # A source the collector gave up on is the loudest thing on this screen:
        # it stopped producing and nothing else says so.
        status = f"⚠️ {state['errored']} failing"
    elif state["paused"]:
        status = f"⏸ {state['paused']} paused"
    elif quiet:
        status = f"💤 {len(quiet)} quiet"
    else:
        status = "all clear"

    lines = [f"<b>🛰 Sentinel</b>  <i>· {status}</i>", ""]

    if state["next"]:
        slot, minutes = state["next"]
        lines.append(f"Next digest <b>{slot}</b> · in {_countdown(minutes)}")
    else:
        lines.append("Next digest <b>—</b> · set one in 🕐 Timetable")

    cats = state["categories"]
    srcs = state["sources"]
    lines.append(
        f"<b>{state['pending']}</b> waiting · "
        f"{cats} categor{'y' if cats == 1 else 'ies'} · "
        f"{srcs} source{'' if srcs == 1 else 's'}"
    )

    last = state["last_digest"]
    if last:
        issue = f"#{last['issue']} " if last["issue"] else ""
        count = last["items"]
        lines.append(
            f"Last: <b>{issue}{last['time']}</b> · {count} item{'' if count == 1 else 's'}"
        )

    if quiet:
        names = " · ".join(
            f"{escape(name)} {f'{days}d' if days is not None else 'never'}" for name, days in quiet[:3]
        )
        more = f" +{len(quiet) - 3}" if len(quiet) > 3 else ""
        lines.append("")
        lines.append(f"<i>💤 quiet 5+ days: {names}{more}</i>")

    return "\n".join(lines)


def home_keyboard(state: dict) -> InlineKeyboardMarkup:
    """Counts ride on the buttons: home is the screen opened on every visit, and
    "5 categories, 12 filters" is most of what a check-in asks."""
    filters_label = f"🚫 Filters · {state['filters']}" if state["filters"] else "🚫 Filters"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"📚 Categories · {state['categories']}", callback_data="cat_list"),
            InlineKeyboardButton("🕐 Timetable", callback_data="tt_list"),
        ],
        [
            InlineKeyboardButton(filters_label, callback_data="blocked_list"),
            InlineKeyboardButton("📊 Stats", callback_data="home_stats"),
        ],
        [
            InlineKeyboardButton("▶️ Send digest now", callback_data="home_digest"),
            InlineKeyboardButton("📄", callback_data="home_logs"),
        ],
    ])


async def gather_home_state() -> dict:
    cats = await get_categories()
    active = await get_active_sources()
    pending_sources = await get_pending_sources()
    errored = await get_error_sources()
    paused = await get_paused_sources()
    silent = await get_silent_sources(_QUIET_THRESHOLD_HOURS)
    last = await get_last_digest()

    tz = ZoneInfo(settings.digest_timezone)
    now = datetime.now(tz)
    last_digest = None
    if last:
        sent = local_sent_at(last["sent_at"], tz)
        if sent:
            last_digest = {
                "issue": sent.timetuple().tm_yday,
                "time": sent.strftime("%H:%M"),
                "items": last["items_total"],
            }

    return {
        "next": next_fire(list(slots_by_time(cats)), now),
        "pending": await count_unsent_items(),
        "categories": len(cats),
        "sources": len(active) + len(pending_sources) + len(errored) + len(paused),
        "errored": len(errored),
        "paused": len(paused),
        "filters": len(await get_blocked_words()),
        "last_digest": last_digest,
        # hours_silent is NULL for a source that has never produced an item — that
        # is "never", not "0 days ago".
        "quiet": [(row["name"], row["hours_silent"] // 24 if row["hours_silent"] is not None else None)
                  for row in silent],
    }


async def render_home() -> tuple[str, InlineKeyboardMarkup]:
    state = await gather_home_state()
    return home_text(state), home_keyboard(state)

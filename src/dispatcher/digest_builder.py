import logging
from datetime import datetime, timezone
from html import escape

from src.db.models import get_categories, get_unsent_items, log_digest, mark_sent
from src.dispatcher.sender import send_message

log = logging.getLogger(__name__)

_TELEGRAM_LIMIT = 4000


def _format_high(item) -> str:
    url = item["original_url"] or ""
    link = f' <a href="{url}">→</a>' if url else ""
    return f"• {escape(item['summary'] or '')}{link}"


def _format_low(item) -> str:
    url = item["original_url"] or ""
    title = escape((item["raw_text"] or "")[:80].split("\n")[0])
    return f'• <a href="{url}">{title}</a>' if url else f"• {title}"


def _build_digest_text(sections: dict[str, dict], date_str: str) -> list[str]:
    lines = [f"<b>📋 Digest — {date_str}</b>"]

    for cat_name, data in sections.items():
        emoji = data["emoji"]
        high = data["high"]
        low = data["low"]

        if not high and not low:
            continue

        lines.append(f"\n<b>{emoji} {cat_name.capitalize()}</b>")
        for item in high:
            lines.append(_format_high(item))
        for item in low:
            lines.append(_format_low(item))

    return lines


def _split_into_messages(lines: list[str]) -> list[str]:
    messages, current = [], ""
    for line in lines:
        candidate = (current + "\n" + line).lstrip("\n")
        if len(candidate) > _TELEGRAM_LIMIT:
            if current:
                messages.append(current)
            current = line
        else:
            current = candidate
    if current:
        messages.append(current)
    return messages


async def send_digest() -> bool:
    items = await get_unsent_items()
    if not items:
        log.info("Digest triggered: no unsent items")
        return False

    categories = await get_categories()
    cat_meta = {row["name"]: {"emoji": row["emoji"], "high": [], "low": []} for row in categories}

    for item in items:
        cat = item["category"] or "other"
        if cat not in cat_meta:
            cat_meta[cat] = {"emoji": "📌", "high": [], "low": []}
        bucket = "high" if item["importance"] == "high" else "low"
        cat_meta[cat][bucket].append(item)

    date_str = datetime.now(timezone.utc).strftime("%d %B %Y")
    lines = _build_digest_text(cat_meta, date_str)
    messages = _split_into_messages(lines)

    failed = False
    for msg in messages:
        try:
            await send_message(msg)
        except Exception as exc:
            log.error("Failed to send digest message: %s", exc)
            failed = True
            break

    if failed:
        return False

    sent_ids = [item["id"] for item in items]
    await mark_sent(sent_ids)

    high_count = sum(len(d["high"]) for d in cat_meta.values())
    low_count = sum(len(d["low"]) for d in cat_meta.values())
    await log_digest(total=len(items), high=high_count, low=low_count)

    log.info(
        "Digest sent: %d total | %d high | %d low | %d message(s)",
        len(items), high_count, low_count, len(messages),
    )
    return True

import logging
from collections import defaultdict
from datetime import datetime, timezone
from html import escape

from src.db.models import get_categories, get_unsent_items, log_digest, mark_sent
from src.dispatcher.sender import send_message

log = logging.getLogger(__name__)

_TELEGRAM_LIMIT = 4000


def _format_item(item) -> str:
    url = item["original_url"] or ""
    summary = escape(item["summary"] or (item["raw_text"] or "")[:60].split("\n")[0])
    hour = ""
    if item["published_at"]:
        try:
            hour = f"{datetime.fromisoformat(item['published_at']).hour}⌚ "
        except Exception:
            pass
    text = f"{hour}{summary}"
    return f'<a href="{url}">{text}</a>' if url else text


def _build_digest_text(cat_meta: dict, date_str: str) -> list[str]:
    lines = [f"<b>📋 Digest — {date_str}</b>"]

    for cat_name, data in cat_meta.items():
        sources = data["sources"]
        if not any(sources.values()):
            continue

        lines.append(f"\n<b>{data['emoji']} {cat_name.capitalize()}</b>")

        block_lines = []
        for source_name, source_items in sources.items():
            if not source_items:
                continue
            if block_lines:
                block_lines.append("")
            block_lines.append(f"<b>{escape(source_name)}</b>")
            for item in source_items:
                block_lines.append(_format_item(item))
        if block_lines:
            lines.append("<blockquote expandable>" + "\n".join(block_lines) + "</blockquote>")

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
    cat_meta = {
        row["name"]: {"emoji": row["emoji"], "sources": defaultdict(list)}
        for row in categories
    }

    for item in items:
        cat = item["category"] or "other"
        if cat not in cat_meta:
            cat_meta[cat] = {"emoji": "📌", "sources": defaultdict(list)}
        source_name = item["source_name"] or "Unknown"
        cat_meta[cat]["sources"][source_name].append(item)

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

    await log_digest(total=len(items), high=0, low=0)
    log.info("Digest sent: %d total | %d message(s)", len(items), len(messages))
    return True

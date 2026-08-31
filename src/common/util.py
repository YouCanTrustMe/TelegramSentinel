"""Small shared helpers with no project dependencies."""


def row_get(row, key, default=None):
    """Optional-column access for aiosqlite.Row (which lacks .get()) and dict rows alike."""
    return row[key] if key in row.keys() else default


def needs_summary(item) -> bool:
    """True when an item has no usable summary yet but has raw text to build one from."""
    return not (item["summary"] or "").strip() and bool((item["raw_text"] or "").strip())


def source_link(type_: str, url: str) -> str | None:
    """Clickable link for a source: a Telegram handle becomes a t.me link, an RSS
    feed links straight to its url. None when there is nothing linkable — a numeric
    or empty handle (a private chat id) has no public page."""
    url = (url or "").strip()
    if not url:
        return None
    if type_ == "telegram" and not url.startswith("http"):
        handle = url.lstrip("@")
        return f"https://t.me/{handle}" if handle[:1].isalpha() else None
    return url if url.startswith("http") else None


def ago(hours: float | None) -> str:
    """"40m ago" / "6h ago" / "11d ago" — a source list only ever needs one unit."""
    if hours is None:
        return "never"
    if hours < 1:
        return f"{max(int(hours * 60), 1)}m ago"
    if hours < 48:
        return f"{int(hours)}h ago"
    return f"{int(hours // 24)}d ago"

"""Small shared helpers with no project dependencies."""


def row_get(row, key, default=None):
    """Optional-column access for aiosqlite.Row (which lacks .get()) and dict rows alike."""
    return row[key] if key in row.keys() else default


def plain_text(value) -> str:
    """Telegram text as a str: pyrogram 2.0 (layer 158) hands over a str, newer forks wrap
    it in TextWithEntities / FormattedText, whose .text holds it and may be None. Not
    str(value): FormattedText.__str__ returns that None and raises."""
    text = getattr(value, "text", value)
    return "" if text is None else str(text)


def needs_summary(item) -> bool:
    """True when an item has no usable summary yet but has raw text to build one from."""
    return not (item["summary"] or "").strip() and bool((item["raw_text"] or "").strip())


def _lines(text: str) -> list[str]:
    return [line.strip() for line in (text or "").split("\n") if line.strip()]


def detect_footers(texts: list[str], min_count: int = 5, rounds: int = 3) -> set[str]:
    """Lines a channel signs its posts with: a last line that ends at least `min_count`
    posts, under different text in at least half of them. Found from the posts
    themselves, so a new channel's signature needs no list. The different-text share
    keeps a template channel's recurring line ("Threat: drones" / "Kyiv region") —
    which is the news — from passing for a signature: on prod, real signatures sat under
    different text in 81-100% of posts, a template line in 12%. Several rounds, because some sign with two lines
    ("➕ Subscribe", then "📸 Send news") and the inner one only becomes last once the
    outer is gone."""
    footers: set[str] = set()
    for _ in range(rounds):
        above: dict[str, set[str]] = {}
        ends: dict[str, int] = {}
        for text in texts:
            lines = _lines(text)
            while len(lines) > 1 and lines[-1] in footers:
                lines.pop()
            if len(lines) > 1:
                above.setdefault(lines[-1], set()).add("\n".join(lines[:-1]))
                ends[lines[-1]] = ends.get(lines[-1], 0) + 1
        found = {line for line, bodies in above.items()
                 if len(bodies) >= min_count and len(bodies) * 2 >= ends[line]}
        if found <= footers:
            break
        footers |= found
    return footers


def strip_footers(text: str, footers: set[str]) -> str:
    """Drop trailing signature lines, never the post's only line."""
    lines = (text or "").rstrip().split("\n")
    while sum(1 for line in lines if line.strip()) > 1 and (not lines[-1].strip() or lines[-1].strip() in footers):
        lines.pop()
    return "\n".join(lines).rstrip()


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

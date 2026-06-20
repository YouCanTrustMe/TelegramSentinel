"""Small shared helpers with no project dependencies."""


def row_get(row, key, default=None):
    """Optional-column access for aiosqlite.Row (which lacks .get()) and dict rows alike."""
    return row[key] if key in row.keys() else default


def needs_summary(item) -> bool:
    """True when an item has no usable summary yet but has raw text to build one from."""
    return not (item["summary"] or "").strip() and bool((item["raw_text"] or "").strip())

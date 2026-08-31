"""The stats screen.

Split the same way as the home screen: `stats_text` takes plain values, so what
the admin reads is testable without a database. What changed against the old
version is the question it answers — not only what came in, but what stopped
coming in, which is why the quiet block leads.
"""
from html import escape

from src.db.models import get_categories, get_db, get_silent_sources

_QUIET_THRESHOLD_HOURS = 120


def stats_text(state: dict) -> str:
    lines = [
        f"<b>📊 Last 24h</b> · {state['total']} collected · {state['pending']} pending"
    ]

    if state["quiet"]:
        lines.append("")
        lines.append("<i>⏸ Quiet</i>")
        lines.append("   ·   ".join(
            f"{escape(name)} · {f'{days}d' if days is not None else 'never'}"
            for name, days in state["quiet"]
        ))

    for cat in state["categories"]:
        header = f"<b>{cat['emoji']} {escape(cat['name'])}</b> · {cat['total']}"
        if cat["muted"]:
            header += f" · {cat['muted']} muted as dupes"
        sources = " · ".join(
            f"{escape(s['name'])} <b>{s['count']}</b>" + (f" ⏳{s['unsent']}" if s["unsent"] else "")
            for s in cat["sources"]
        )
        lines.append(f"<blockquote expandable>{header}\n{sources}</blockquote>")

    if not state["categories"] and not state["quiet"]:
        lines.append("")
        lines.append("<i>Nothing collected in the last 24 hours.</i>")
    return "\n".join(lines)


async def gather_stats() -> dict:
    async with get_db() as db:
        async with db.execute(
            "SELECT COUNT(*) AS total FROM items "
            "WHERE julianday(processed_at) >= julianday('now', '-24 hours')"
        ) as cur:
            total = (await cur.fetchone())["total"]
        async with db.execute("SELECT COUNT(*) AS unsent FROM items WHERE sent = 0") as cur:
            pending = (await cur.fetchone())["unsent"]
        async with db.execute(
            """SELECT sources.name, sources.category,
                      SUM(CASE WHEN julianday(items.processed_at) >= julianday('now', '-24 hours')
                               THEN 1 ELSE 0 END) AS cnt,
                      SUM(CASE WHEN items.sent = 0 THEN 1 ELSE 0 END) AS unsent_cnt,
                      SUM(CASE WHEN items.duplicate_of IS NOT NULL
                                AND julianday(items.processed_at) >= julianday('now', '-24 hours')
                               THEN 1 ELSE 0 END) AS muted_cnt
               FROM items JOIN sources ON items.source_id = sources.id
               LEFT JOIN categories ON sources.category = categories.name
               GROUP BY sources.id
               HAVING cnt > 0 OR unsent_cnt > 0
               ORDER BY COALESCE(categories.sort_order, 999), sources.category, cnt DESC"""
        ) as cur:
            rows = await cur.fetchall()

    emoji = {c["name"]: c["emoji"] for c in await get_categories()}
    by_cat: dict[str, dict] = {}
    for row in rows:
        cat = by_cat.setdefault(row["category"], {
            "name": row["category"], "emoji": emoji.get(row["category"], "📌"),
            "total": 0, "muted": 0, "sources": [],
        })
        cat["total"] += row["cnt"]
        cat["muted"] += row["muted_cnt"] or 0
        cat["sources"].append({"name": row["name"], "count": row["cnt"], "unsent": row["unsent_cnt"]})

    silent = await get_silent_sources(_QUIET_THRESHOLD_HOURS)
    return {
        "total": total,
        "pending": pending,
        "quiet": [(row["name"], row["hours_silent"] // 24 if row["hours_silent"] is not None else None)
                  for row in silent],
        "categories": list(by_cat.values()),
    }


async def render_stats() -> str:
    return stats_text(await gather_stats())

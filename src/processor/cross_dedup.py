"""Cross-source deduplication: detect when several sources report the same story
and keep only one ("primary"), muting the rest. Runs at digest time on the
finalized Ukrainian summaries (cleaner, single-language input than raw ingest
text → far fewer false positives, which here mean silently dropping a real
story). Clustering is per category via embedding cosine similarity; the embedding
transport lives in embedder.py.

Two safety nets:
- fail-open: any error returns all items unchanged, never drops anything.
- shadow mode (settings.dedup_shadow): logs would-be duplicates without hiding
  them, so the threshold can be validated on real digests before enforcing.
"""
import logging
from collections import defaultdict

import numpy as np

from src.config import settings
from src.db.models import (
    get_duplicate_links,
    get_recent_embedded_items,
    mark_duplicate,
    set_item_embedding,
)
from src.processor.embedder import cosine, embed_texts, from_blob, to_blob

log = logging.getLogger(__name__)


def _field(item, key, default=None):
    """Read a field from either an aiosqlite.Row or a plain dict (the digest
    pipeline turns some rows into dicts during reclassify)."""
    try:
        val = item[key]
    except (KeyError, IndexError):
        return default
    return default if val is None else val


def _sort_key(item) -> tuple[float, str]:
    """Primary selection: lowest source sort_order (highest user priority, same
    order the digest renders in), tie-broken by earliest published_at."""
    so = _field(item, "source_sort_order")
    so = so if isinstance(so, (int, float)) else 1e9
    return (so, _field(item, "published_at", "9999") or "9999")


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[int, int] = {}

    def find(self, x: int) -> int:
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[ra] = rb


async def deduplicate(items: list) -> tuple[list, dict[int, list[tuple[str, str]]]]:
    """Return (surviving_items, dup_link_map). dup_link_map maps a surviving
    primary's id to the (source_name, url) of duplicates muted under it."""
    try:
        return await _deduplicate(items)
    except Exception:
        log.exception("Cross-source dedup failed, sending all items unchanged")
        return list(items), {}


async def _deduplicate(items: list) -> tuple[list, dict[int, list[tuple[str, str]]]]:
    items = list(items)
    if len(items) < 2:
        return items, {}

    # 1. Embed items that have no stored vector yet (on the finalized summary).
    vec: dict[int, np.ndarray] = {}
    to_embed: list[tuple[int, str]] = []
    for item in items:
        iid = _field(item, "id")
        if iid is None:
            continue
        existing = from_blob(_field(item, "embedding"))
        if existing is not None:
            vec[iid] = existing
            continue
        text = (_field(item, "summary", "") or _field(item, "raw_text", "") or "").strip()
        if text:
            to_embed.append((iid, text))
    if to_embed:
        vectors = await embed_texts([t for _, t in to_embed])
        for (iid, _), v in zip(to_embed, vectors):
            if v is not None:
                arr = np.asarray(v, dtype=np.float32)
                vec[iid] = arr
                await set_item_embedding(iid, to_blob(arr))

    if len(vec) < 2:
        return items, {}

    item_by_id = {_field(item, "id"): item for item in items}
    current_ids = set(item_by_id)

    # 2. Comparison pool: items already embedded and SENT within the window, so a
    # new item can match one shown in a previous digest (not only this batch).
    window = await get_recent_embedded_items(settings.dedup_window_hours)
    sent_pool: dict[str, list[tuple[int, np.ndarray, object, object]]] = defaultdict(list)
    for row in window:
        if row["id"] in current_ids or not row["sent"]:
            continue
        v = from_blob(row["embedding"])
        if v is not None:
            sent_pool[row["category"] or "other"].append(
                (row["id"], v, row["source_sort_order"], row["published_at"])
            )

    # 3. Cluster per category (a cross-source duplicate is always same-category).
    by_cat: dict[str, list] = defaultdict(list)
    for item in items:
        if _field(item, "id") in vec:
            by_cat[_field(item, "category", "other") or "other"].append(item)

    muted: dict[int, int] = {}  # duplicate id -> primary id (primary may be a sent-pool id)
    for cat, cat_items in by_cat.items():
        cur = [(_field(it, "id"), vec[_field(it, "id")]) for it in cat_items]
        pool = sent_pool.get(cat, [])
        uf = _UnionFind()
        for a in range(len(cur)):
            ida, va = cur[a]
            uf.find(ida)
            for b in range(a + 1, len(cur)):
                idb, vb = cur[b]
                if cosine(va, vb) >= settings.dedup_threshold:
                    uf.union(ida, idb)
        sent_nodes: set[int] = set()
        for ida, va in cur:
            for sid, vs, _so, _pub in pool:
                if cosine(va, vs) >= settings.dedup_threshold:
                    uf.union(ida, sid)
                    sent_nodes.add(sid)

        comps: dict[int, list[int]] = defaultdict(list)
        for ida, _ in cur:
            comps[uf.find(ida)].append(ida)
        comp_sent: dict[int, list[int]] = defaultdict(list)
        for sid in sent_nodes:
            comp_sent[uf.find(sid)].append(sid)

        for root, members in comps.items():
            already_sent = comp_sent.get(root, [])
            if len(members) == 1 and not already_sent:
                continue
            if already_sent:
                # Story already delivered in a past digest → mute every current
                # member, no link (the primary the user saw is not in this digest).
                primary = already_sent[0]
                for mid in members:
                    muted[mid] = primary
            else:
                primary = min(members, key=lambda i: _sort_key(item_by_id[i]))
                for mid in members:
                    if mid != primary:
                        muted[mid] = primary

    if not muted:
        log.info("Cross-source dedup: no duplicates among %d item(s)", len(items))
        return items, {}

    for mid, pid in muted.items():
        it = item_by_id.get(mid)
        log.info(
            "%s cross-source duplicate: item id=%d (%s) -> primary id=%d | summary=%s",
            "WOULD-MUTE" if settings.dedup_shadow else "Muting",
            mid, _field(it, "source_name", "?"), pid, (_field(it, "summary", "") or "")[:80],
        )

    if settings.dedup_shadow:
        log.info("Cross-source dedup SHADOW: %d duplicate(s) detected, nothing hidden", len(muted))
        return items, {}

    for mid, pid in muted.items():
        await mark_duplicate(mid, pid)
    survivors = [it for it in items if _field(it, "id") not in muted]
    link_map = await get_duplicate_links([_field(it, "id") for it in survivors])
    log.info("Cross-source dedup: muted %d duplicate(s), %d survivor(s)", len(muted), len(survivors))
    return survivors, link_map

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
    set_item_embeddings,
)
from src.processor.llm.classifier import group_by_topic
from src.common.util import row_get
from src.processor.dedup.embedder import cosine, embed_texts, from_blob, to_blob

log = logging.getLogger(__name__)


_PLACEHOLDER_SUMMARIES = {"no text", "no caption", "media"}

# Max items (primary + candidates) the LLM judges together for ONE primary. Bigger
# groups make the LLM over-group and risk muting a distinct story; large groups are chunked.
_B1_MAX_GROUP = 10

# Several SMALL primaries are packed into one group_by_topic call up to this many items
# (distinct primaries = distinct events, so this does not raise the per-primary over-group
# risk). Without this, a big digest fires one call PER primary (~20), and on a 5 RPM
# provider that throttles the whole digest to minutes. Two chunks of the SAME primary are
# never packed together, so _B1_MAX_GROUP still bounds how many candidates one primary sees.
_B1_CONFIRM_BATCH = 18


def _is_placeholder(text: str) -> bool:
    """Media-only / empty-caption summaries (e.g. 'no text') are identical across
    unrelated posts, so they embed to cosine 1.0 and would be falsely clustered.
    Exclude them from embedding entirely."""
    t = text.strip().lower()
    if t in _PLACEHOLDER_SUMMARIES:
        return True
    return t.startswith("[") and t.endswith("]") and len(t) <= 20


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


async def ensure_embeddings(items: list) -> dict[int, np.ndarray]:
    """Return {item_id: vector} for the given items, embedding (and persisting)
    any that lack a stored vector. Computed once per digest and shared by both
    cross-source dedup and within-source merge. Fail-open: items that can't be
    embedded are simply absent from the map."""
    vec: dict[int, np.ndarray] = {}
    to_embed: list[tuple[int, str]] = []
    for item in items:
        iid = _field(item, "id")
        if iid is None:
            continue
        text = (_field(item, "summary", "") or _field(item, "raw_text", "") or "").strip()
        if not text or _is_placeholder(text):
            continue
        existing = from_blob(_field(item, "embedding"))
        if existing is not None:
            vec[iid] = existing
            continue
        to_embed.append((iid, text))
    if to_embed:
        vectors = await embed_texts([t for _, t in to_embed])
        new_blobs: list[tuple[int, bytes]] = []
        for (iid, _), v in zip(to_embed, vectors):
            if v is not None:
                arr = np.asarray(v, dtype=np.float32)
                vec[iid] = arr
                new_blobs.append((iid, to_blob(arr)))
        await set_item_embeddings(new_blobs)
    return vec


def cluster_within_source(items: list, vectors: dict[int, np.ndarray], threshold: float) -> list[list]:
    """Group one source's items into same-event clusters by embedding cosine.
    Returns a list of clusters (each a list of items); items without a vector
    are returned as their own singleton cluster."""
    with_vec = [it for it in items if _field(it, "id") in vectors]
    without = [it for it in items if _field(it, "id") not in vectors]
    uf = _UnionFind()
    for a in range(len(with_vec)):
        ida = _field(with_vec[a], "id")
        uf.find(ida)
        va = vectors[ida]
        for b in range(a + 1, len(with_vec)):
            idb = _field(with_vec[b], "id")
            if cosine(va, vectors[idb]) >= threshold:
                uf.union(ida, idb)
    groups: dict[int, list] = defaultdict(list)
    for it in with_vec:
        groups[uf.find(_field(it, "id"))].append(it)
    clusters = list(groups.values()) + [[it] for it in without]
    return clusters


async def deduplicate(items: list, vectors: dict[int, np.ndarray]) -> tuple[list, dict[int, list[tuple[str, str]]]]:
    """Return (surviving_items, dup_link_map). dup_link_map maps a surviving
    primary's id to the (source_name, url) of duplicates muted under it.
    `vectors` is the shared embedding map from ensure_embeddings()."""
    try:
        return await _deduplicate(items, vectors)
    except Exception:
        log.exception("Cross-source dedup failed, sending all items unchanged")
        return list(items), {}


async def _confirm_mutes(
    muted: dict[int, int],
    item_by_id: dict,
    sent_summary: dict[int, str],
    vec: dict[int, np.ndarray],
    sent_vec: dict[int, np.ndarray],
) -> dict[int, int]:
    """B1 — LLM confirmation before muting. Embeddings only pre-select candidates;
    in high-overlap domains (war/strike news) DIFFERENT cross-source events score
    the same cosine as the SAME event, and muting hides a real story for good. So
    each candidate is confirmed by the LLM (the same group_by_topic arbiter the
    within-source merge uses) — only items it groups WITH the primary stay muted.
    Near-identical pairs (>= merge_near_dup_threshold) are certain dups and skip
    the LLM. Fail-open: an LLM error keeps the items (no mute)."""
    by_primary: dict[int, list[int]] = defaultdict(list)
    for mid, pid in muted.items():
        by_primary[pid].append(mid)

    confirmed: dict[int, int] = {}
    # Build the LLM work as "units": each unit is one primary plus a bounded chunk of its
    # candidates (the over-group guard). Near-identical reposts are confirmed here without
    # the LLM.
    units: list[tuple[int, list[dict]]] = []  # (primary_id, group_by_topic inputs incl. the primary)
    for pid, dups in by_primary.items():
        pvec = vec.get(pid)
        if pvec is None:
            pvec = sent_vec.get(pid)
        need_llm: list[int] = []
        for d in dups:
            dv = vec.get(d)
            if dv is not None and pvec is not None and cosine(dv, pvec) >= settings.merge_near_dup_threshold:
                confirmed[d] = pid  # near-identical repost: certain dup, no LLM needed
            else:
                need_llm.append(d)
        if not need_llm:
            continue
        primary_summary = (
            _field(item_by_id[pid], "summary", "") if pid in item_by_id else sent_summary.get(pid, "")
        ) or ""
        chunk_size = max(1, _B1_MAX_GROUP - 1)
        for start in range(0, len(need_llm), chunk_size):
            chunk = need_llm[start:start + chunk_size]
            inputs = [{"id": pid, "text": primary_summary}]
            inputs += [{"id": d, "text": _field(item_by_id[d], "summary", "") or ""} for d in chunk]
            units.append((pid, inputs))

    # Pack units into batches (first-fit). A batch never holds two units of the SAME primary
    # (that would let one primary see more candidates than _B1_MAX_GROUP), and stays within
    # _B1_CONFIRM_BATCH items — so many small primaries share one call instead of one each.
    batches: list[tuple[list[dict], set[int], set[int]]] = []  # (inputs, pids, ids)
    for pid, inputs in units:
        placed = False
        for b_inputs, b_pids, b_ids in batches:
            if pid in b_pids:
                continue
            add = sum(1 for x in inputs if x["id"] not in b_ids)
            if len(b_ids) + add <= _B1_CONFIRM_BATCH:
                for x in inputs:
                    if x["id"] not in b_ids:
                        b_inputs.append(x)
                        b_ids.add(x["id"])
                b_pids.add(pid)
                placed = True
                break
        if not placed:
            batches.append((list(inputs), {pid}, {x["id"] for x in inputs}))

    # One LLM call per batch; collect, per primary, the ids the LLM put in its event group.
    same_group_of: dict[int, set[int]] = defaultdict(set)
    for b_inputs, b_pids, _b_ids in batches:
        try:
            groups = await group_by_topic(b_inputs)
        except Exception as exc:
            log.warning("B1: LLM confirm failed for %d primary group(s), keeping candidates unmuted: %s",
                        len(b_pids), exc)
            continue
        for g in groups:
            ids = set(g.get("ids", []))
            for pid in b_pids & ids:
                same_group_of[pid].update(ids)

    for pid, dups in by_primary.items():
        for d in dups:
            if d in confirmed:  # near-dup auto-confirmed above
                continue
            if d in same_group_of.get(pid, ()):
                confirmed[d] = pid
            else:
                log.info("B1: kept item id=%d — LLM says different event from primary id=%d", d, pid)
    return confirmed


async def _deduplicate(items: list, vec: dict[int, np.ndarray]) -> tuple[list, dict[int, list[tuple[str, str]]]]:
    items = list(items)
    if len(items) < 2 or len(vec) < 2:
        return items, {}

    item_by_id = {_field(item, "id"): item for item in items}
    current_ids = set(item_by_id)

    # 2. Comparison pool: items already embedded and SENT within the window, so a
    # new item can match one shown in a previous digest (not only this batch).
    window = await get_recent_embedded_items(settings.dedup_window_hours)
    sent_pool: dict[str, list[tuple[int, np.ndarray, object, object]]] = defaultdict(list)
    sent_vec: dict[int, np.ndarray] = {}
    sent_summary: dict[int, str] = {}
    for row in window:
        if row["id"] in current_ids or not row["sent"]:
            continue
        v = from_blob(row["embedding"])
        if v is not None:
            sent_pool[row["category"] or "other"].append(
                (row["id"], v, row["source_sort_order"], row["published_at"])
            )
            sent_vec[row["id"]] = v
            sent_summary[row["id"]] = row_get(row, "summary", "") or ""

    floor = settings.dedup_log_floor

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
                c = cosine(va, vb)
                ia, ib = item_by_id[ida], item_by_id[idb]
                if c >= floor and _field(ia, "source_id") != _field(ib, "source_id"):
                    # Per-pair tuning telemetry (O(pairs)) — DEBUG so it doesn't
                    # flood INFO; the actual mute decisions are logged once below.
                    log.debug("DEDUP-CANDIDATE cosine=%.3f tier=%s x-src same-digest [%s] | %s || %s",
                              c, "strong" if c >= settings.dedup_threshold else "confirm",
                              cat, (_field(ia, "summary", "") or "")[:60], (_field(ib, "summary", "") or "")[:60])
                if c >= floor:
                    uf.union(ida, idb)
        sent_nodes: set[int] = set()
        for ida, va in cur:
            for sid, vs, _so, _pub in pool:
                c = cosine(va, vs)
                if c >= floor:
                    log.debug("DEDUP-CANDIDATE cosine=%.3f tier=%s x-digest [%s] | %s || (sent) %s",
                              c, "strong" if c >= settings.dedup_threshold else "confirm",
                              cat, (_field(item_by_id[ida], "summary", "") or "")[:60], sent_summary.get(sid, "")[:60])
                if c >= floor:
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
                primary_src = _field(item_by_id[primary], "source_id")
                for mid in members:
                    # Leave same-source duplicates to the within-source AI merge,
                    # which folds them into one richer summary; cross-source dedup
                    # only collapses the SAME story across DIFFERENT sources.
                    if mid != primary and _field(item_by_id[mid], "source_id") != primary_src:
                        muted[mid] = primary

    if not muted:
        log.info("Cross-source dedup: no duplicates among %d item(s)", len(items))
        return items, {}

    for mid, pid in muted.items():
        it = item_by_id.get(mid)
        primary_vec = vec.get(pid)
        if primary_vec is None:
            primary_vec = sent_vec.get(pid)
        score = f"{cosine(vec[mid], primary_vec):.3f}" if (mid in vec and primary_vec is not None) else "n/a"
        log.info(
            "%s cross-source duplicate: item id=%d (%s) -> primary id=%d | cosine=%s | summary=%s",
            "WOULD-MUTE" if settings.dedup_shadow else "Candidate",
            mid, _field(it, "source_name", "?"), pid, score, (_field(it, "summary", "") or "")[:80],
        )

    if settings.dedup_shadow:
        log.info("Cross-source dedup SHADOW: %d duplicate(s) detected, nothing hidden", len(muted))
        return items, {}

    candidates = len(muted)
    muted = await _confirm_mutes(muted, item_by_id, sent_summary, vec, sent_vec)
    if not muted:
        log.info("Cross-source dedup: %d candidate(s) all rejected by LLM, nothing muted", candidates)
        return items, {}

    for mid, pid in muted.items():
        await mark_duplicate(mid, pid)
    survivors = [it for it in items if _field(it, "id") not in muted]
    link_map = await get_duplicate_links([_field(it, "id") for it in survivors])
    log.info("Cross-source dedup: muted %d/%d candidate(s) after LLM confirm, %d survivor(s)",
             len(muted), candidates, len(survivors))
    return survivors, link_map

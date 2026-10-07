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
import re
from collections import Counter, defaultdict

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


# Characters of post text per richness step. Coarse on purpose: a channel signature or
# a hashtag line must not outrank the sort order, a paragraph of detail must.
_RICHNESS_STEP = 150
_URL_RE = re.compile(r"https?://\S+")
_BRACKET_PREFIX_RE = re.compile(r"^\s*(\[[^\]]{0,300}\]\s*)+")


def _richness(item) -> int:
    """How much the post itself says, in _RICHNESS_STEP steps. The summaries are all
    capped to one line, so the raw text is what tells a one-line repost ("Poland and
    Romania refused the grain transit") from the report that also says why."""
    text = _field(item, "raw_text", "") or ""
    text = _BRACKET_PREFIX_RE.sub("", _URL_RE.sub("", text))
    return len(" ".join(text.split())) // _RICHNESS_STEP


def _sort_key(item) -> tuple[int, int, float, str]:
    """Primary selection: a Telegram source first (a t.me original opens in the app in
    one tap, where an RSS original is a browser trip and often a paywall), then the
    post that says the most (_richness), then lowest source sort_order (highest user
    priority, same order the digest renders in), tie-broken by earliest published_at.
    Set primary_prefers_telegram=false to let richness and sort_order decide."""
    tg = 0 if (settings.primary_prefers_telegram
               and _field(item, "source_type") == "telegram") else 1
    so = _field(item, "source_sort_order")
    so = so if isinstance(so, (int, float)) else 1e9
    return (tg, -_richness(item), so, _field(item, "published_at", "9999") or "9999")


def _union_floor(cat_a: str, cat_b: str) -> float:
    """Cosine a pair must clear to become a candidate: the usual floor within one
    category, a stricter one across two, where shared war vocabulary links unrelated
    feed and finance posts."""
    return settings.dedup_log_floor if cat_a == cat_b else settings.dedup_cross_category_threshold


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

    # One LLM call per batch; collect, per primary, the ids the LLM put in its event group,
    # and keep EVERY group it returned (see _regroup_rejected).
    same_group_of: dict[int, set[int]] = defaultdict(set)
    event_groups: list[set[int]] = []
    for b_inputs, b_pids, _b_ids in batches:
        try:
            groups = await group_by_topic(b_inputs)
        except Exception as exc:
            log.warning("B1: LLM confirm failed for %d primary group(s), keeping candidates unmuted: %s",
                        len(b_pids), exc)
            continue
        for g in groups:
            ids = set(g.get("ids", []))
            event_groups.append(ids)
            for pid in b_pids & ids:
                same_group_of[pid].update(ids)

    rejected: list[tuple[int, int]] = []  # (candidate id, the primary it was compared against)
    for pid, dups in by_primary.items():
        for d in dups:
            if d in confirmed:  # near-dup auto-confirmed above
                continue
            if d in same_group_of.get(pid, ()):
                confirmed[d] = pid
            else:
                rejected.append((d, pid))

    confirmed.update(_regroup_rejected(rejected, event_groups, item_by_id, vec))
    return confirmed


def _regroup_rejected(
    rejected: list[tuple[int, int]],
    event_groups: list[set[int]],
    item_by_id: dict,
    vec: dict[int, np.ndarray],
) -> dict[int, int]:
    """Second reading of the SAME partition, no extra LLM call.

    Union-find chains a cluster transitively (A~B~C), but the confirm step only asks
    "is this candidate the same event as the PRIMARY?". When the primary is the weak
    link, every candidate is rejected and the whole cluster survives — even where the
    candidates are plainly the same event as EACH OTHER (observed on prod: two sources
    on one downed Ka-27, cosine 0.966, both delivered because both were compared only
    against an unrelated primary). group_by_topic returns a full partition, so the
    groups that hold no primary are exactly those pairings; use them.

    Embeddings stay the gate: two rejected candidates are only collapsed when their own
    cosine also clears dedup_log_floor, so an LLM that over-groups cannot mute a pair
    the vectors never linked. Category is a gate too: one confirm call carries primaries
    from several categories, so a group can span them, and this second reading has no
    pairing of its own to hold a cross-category pair to the stricter
    dedup_cross_category_threshold the floor pass applies."""
    if not rejected:
        return {}
    primary_of = dict(rejected)
    uf = _UnionFind()
    for ids in event_groups:
        by_cat: dict[str, list[int]] = defaultdict(list)
        for i in ids:
            if i in primary_of:
                by_cat[_field(item_by_id[i], "category", "other") or "other"].append(i)
        for members in by_cat.values():
            for other in members[1:]:
                uf.union(members[0], other)

    comps: dict[int, list[int]] = defaultdict(list)
    for d, _pid in rejected:
        comps[uf.find(d)].append(d)

    out: dict[int, int] = {}
    for members in comps.values():
        if len(members) < 2:
            continue
        survivor = min(members, key=lambda i: _sort_key(item_by_id[i]))
        survivor_src = _field(item_by_id[survivor], "source_id")
        svec = vec.get(survivor)
        for mid in members:
            if mid == survivor or _field(item_by_id[mid], "source_id") == survivor_src:
                continue
            mvec = vec.get(mid)
            if svec is None or mvec is None:
                continue
            score = cosine(mvec, svec)
            if score < settings.dedup_log_floor:
                continue
            out[mid] = survivor
            log.info("B1: regrouped item id=%d -> primary id=%d | cosine=%.3f | same LLM event group, "
                     "though both were rejected against their own primary", mid, survivor, score)

    for d, pid in rejected:
        if d not in out:
            log.info("B1: kept item id=%d — LLM says different event from primary id=%d", d, pid)
    return out


def _sent_matches(cur: list[tuple[int, np.ndarray]], sent_meta: dict, item_by_id: dict) -> list[tuple[int, int, float]]:
    """(current id, sent id, cosine) for each current item's best match in the sent pool.

    Each item is compared with the pool DIRECTLY, never through a chain of current items —
    union-find once muted a "$81K Bitcoin" post at cosine 0.755 under a CLARITY Act one it
    was only linked to via a third post. A post from the SAME source as the shown one is
    that channel's next development of the story (a court ruling, a new death toll), so
    only a near-identical repost of it counts: on 2026-09-18..10-07 such mutes were 42% of
    all, and 26 of 30 checked by hand were new events or new facts. One matrix product,
    since the pool spans every category (~1500 items against ~150)."""
    if not cur or not sent_meta:
        return []
    # One length per model (from_blob already drops the others); the majority guards the
    # product against a stray vector instead of trusting whichever item came first.
    dim = Counter(v.shape for _, v in cur).most_common(1)[0][0]
    sids = [sid for sid, (v, _c, _s) in sent_meta.items() if v.shape == dim]
    rows = [(iid, v) for iid, v in cur if v.shape == dim]
    if not sids or not rows:
        return []

    def _unit(m: np.ndarray) -> np.ndarray:
        n = np.linalg.norm(m, axis=1, keepdims=True)
        return m / np.where(n == 0, 1, n)

    sims = _unit(np.stack([v for _, v in rows])) @ _unit(np.stack([sent_meta[s][0] for s in sids])).T
    lowest = min(settings.dedup_log_floor, settings.dedup_cross_category_threshold)
    out = []
    for r, (iid, _v) in enumerate(rows):
        item = item_by_id[iid]
        cat, src = _field(item, "category", "other") or "other", _field(item, "source_id")
        best: tuple[float, int] | None = None
        for k in np.nonzero(sims[r] >= lowest)[0]:
            sid, c = sids[k], float(sims[r, k])
            _v, s_cat, s_src = sent_meta[sid]
            if c < _union_floor(cat, s_cat) or (s_src == src and c < settings.merge_near_dup_threshold):
                continue
            if best is None or c > best[0]:
                best = (c, sid)
        if best is not None:
            out.append((iid, best[1], best[0]))
    return out


async def _deduplicate(items: list, vec: dict[int, np.ndarray]) -> tuple[list, dict[int, list[tuple[str, str]]]]:
    items = list(items)
    if len(items) < 2 or len(vec) < 2:
        return items, {}

    item_by_id = {_field(item, "id"): item for item in items}
    current_ids = set(item_by_id)

    # Comparison pool: items already embedded and SENT within the window, so a new item
    # can match one shown in a previous digest (not only this batch).
    window = await get_recent_embedded_items(settings.dedup_window_hours)
    sent_meta: dict[int, tuple[np.ndarray, str, object]] = {}  # id -> (vector, category, source_id)
    sent_vec: dict[int, np.ndarray] = {}
    sent_summary: dict[int, str] = {}
    for row in window:
        if row["id"] in current_ids or not row["sent"]:
            continue
        # Current items with no readable text are never embedded (ensure_embeddings
        # skips them), but a stored vector can still exist — a backfill that predates
        # that guard, say. All placeholders embed to nearly the same point, so letting
        # one into the comparison pool offers the digest a meaningless primary.
        if _is_placeholder(row_get(row, "summary", "") or ""):
            continue
        v = from_blob(row["embedding"])
        if v is not None:
            sent_meta[row["id"]] = (v, row["category"] or "other", row_get(row, "source_id"))
            sent_vec[row["id"]] = v
            sent_summary[row["id"]] = row_get(row, "summary", "") or ""

    near_thr = settings.merge_near_dup_threshold

    def _cat(item) -> str:
        return _field(item, "category", "other") or "other"

    def _src(item):
        return _field(item, "source_id")

    cur_all = [(_field(it, "id"), vec[_field(it, "id")]) for it in items if _field(it, "id") in vec]

    # Collapse cross-source near-identical current items without the LLM: such a pair can
    # also union onto a weakly-related already-sent primary, and the confirm step only
    # compares each member against THAT primary — missing the pair itself.
    near_muted: dict[int, int] = {}
    uf = _UnionFind()
    for a in range(len(cur_all)):
        ida, va = cur_all[a]
        uf.find(ida)
        for b in range(a + 1, len(cur_all)):
            idb, vb = cur_all[b]
            if _src(item_by_id[ida]) != _src(item_by_id[idb]) and cosine(va, vb) >= near_thr:
                uf.union(ida, idb)
    comps: dict[int, list[int]] = defaultdict(list)
    for ida, _ in cur_all:
        comps[uf.find(ida)].append(ida)
    for members in comps.values():
        if len(members) < 2:
            continue
        primary = min(members, key=lambda i: _sort_key(item_by_id[i]))
        psrc = _src(item_by_id[primary])
        for mid in members:
            if mid != primary and _src(item_by_id[mid]) != psrc:
                near_muted[mid] = primary

    # Keep the near-dups resolved above out of the floor pass, or they re-anchor onto a weak sent primary.
    cur = [(iid, v) for iid, v in cur_all if iid not in near_muted]
    muted: dict[int, int] = {}  # duplicate id -> primary id (primary may be a sent-pool id)

    for ida, sid, c in _sent_matches(cur, sent_meta, item_by_id):
        ia = item_by_id[ida]
        log.debug("DEDUP-CANDIDATE cosine=%.3f x-digest [%s] | %s || (sent) %s", c, _cat(ia),
                  (_field(ia, "summary", "") or "")[:60], sent_summary.get(sid, "")[:60])
        # Story already delivered → mute, no link (the primary the user saw is not in this digest).
        muted[ida] = sid

    # Within this digest: only cross-source pairs union. Same-source ones are the
    # within-source merge's job, and letting them union here chained a source's
    # unrelated posts into one cluster. An item matched to the sent pool above stays in:
    # if B1 rejects that match, it must still collapse with its partners in this digest.
    uf = _UnionFind()
    for a in range(len(cur)):
        ida, va = cur[a]
        uf.find(ida)
        for b in range(a + 1, len(cur)):
            idb, vb = cur[b]
            ia, ib = item_by_id[ida], item_by_id[idb]
            if _src(ia) == _src(ib):
                continue
            c = cosine(va, vb)
            if c >= _union_floor(_cat(ia), _cat(ib)):
                # Per-pair tuning telemetry (O(pairs)) — DEBUG so it doesn't
                # flood INFO; the actual mute decisions are logged once below.
                log.debug("DEDUP-CANDIDATE cosine=%.3f tier=%s x-src same-digest [%s/%s] | %s || %s",
                          c, "strong" if c >= settings.dedup_threshold else "confirm", _cat(ia), _cat(ib),
                          (_field(ia, "summary", "") or "")[:60], (_field(ib, "summary", "") or "")[:60])
                uf.union(ida, idb)
    comps = defaultdict(list)
    for ida, _ in cur:
        comps[uf.find(ida)].append(ida)
    # A sent-matched item's fallback primary in this digest, tried only if B1 rejects its
    # sent match — so every mute is confirmed against the very item it hides under.
    fallback: dict[int, int] = {}
    for members in comps.values():
        if len(members) < 2:
            continue
        unshown = [m for m in members if m not in muted] or members
        primary = min(unshown, key=lambda i: _sort_key(item_by_id[i]))
        primary_src = _src(item_by_id[primary])
        for mid in members:
            # Leave same-source duplicates to the within-source AI merge,
            # which folds them into one richer summary; cross-source dedup
            # only collapses the SAME story across DIFFERENT sources.
            if mid == primary or _src(item_by_id[mid]) == primary_src:
                continue
            if mid in muted:
                fallback[mid] = primary
            else:
                muted[mid] = primary

    if not muted and not near_muted:
        log.info("Cross-source dedup: no duplicates among %d item(s)", len(items))
        return items, {}

    for mid, pid in muted.items():
        it = item_by_id.get(mid)
        primary_vec = vec.get(pid)
        if primary_vec is None:
            primary_vec = sent_vec.get(pid)
        score = f"{cosine(vec[mid], primary_vec):.3f}" if (mid in vec and primary_vec is not None) else "n/a"
        log.info(
            "%s cross-source duplicate: item id=%d (%s/%s) -> primary id=%d | cosine=%s | summary=%s",
            "WOULD-MUTE" if settings.dedup_shadow else "Candidate",
            mid, _field(it, "source_name", "?"), _field(it, "category", "?"), pid, score,
            (_field(it, "summary", "") or "")[:80],
        )
    for mid, pid in near_muted.items():
        it = item_by_id.get(mid)
        score = f"{cosine(vec[mid], vec[pid]):.3f}" if (mid in vec and pid in vec) else "n/a"
        log.info(
            "%s near-identical cross-source duplicate: item id=%d (%s) -> primary id=%d | cosine=%s | summary=%s",
            "WOULD-MUTE" if settings.dedup_shadow else "Auto",
            mid, _field(it, "source_name", "?"), pid, score, (_field(it, "summary", "") or "")[:80],
        )

    if settings.dedup_shadow:
        log.info("Cross-source dedup SHADOW: %d duplicate(s) detected, nothing hidden", len(muted) + len(near_muted))
        return items, {}

    candidates = len(muted)
    confirmed = await _confirm_mutes(muted, item_by_id, sent_summary, vec, sent_vec) if muted else {}
    # A fallback whose own primary is being hidden would re-point through it to a shown
    # story the item was never compared with — skip those.
    retry = {mid: pid for mid, pid in fallback.items() if mid not in confirmed and pid not in confirmed}
    if retry:
        candidates += len(retry)
        confirmed.update(await _confirm_mutes(retry, item_by_id, sent_summary, vec, sent_vec))
    confirmed.update(near_muted)
    if not confirmed:
        log.info("Cross-source dedup: %d candidate(s) all rejected by LLM, nothing muted", candidates)
        return items, {}

    muted = confirmed
    # A near-dup primary can itself be muted by the floor pass (Z->X, X->Y); point every
    # muted item at a surviving primary, else Z's source link renders under the hidden X.
    def _survivor(pid: int) -> int:
        seen: set[int] = set()
        while pid in muted and pid not in seen:
            seen.add(pid)
            pid = muted[pid]
        return pid

    muted = {mid: _survivor(pid) for mid, pid in muted.items()}
    for mid, pid in muted.items():
        await mark_duplicate(mid, pid)
    survivors = [it for it in items if _field(it, "id") not in muted]
    link_map = await get_duplicate_links([_field(it, "id") for it in survivors])
    log.info("Cross-source dedup: muted %d LLM-confirmed (of %d) + %d near-identical, %d survivor(s)",
             len(muted) - len(near_muted), candidates, len(near_muted), len(survivors))
    return survivors, link_map

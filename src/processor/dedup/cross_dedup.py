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
from src.processor.llm.classifier import judge_pairs
from src.common.util import row_get
from src.processor.dedup.embedder import cosine, embed_texts, from_blob, to_blob

log = logging.getLogger(__name__)


_PLACEHOLDER_SUMMARIES = {"no text", "no caption", "media"}

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


def _post_text(item) -> str:
    """What the pair judge reads for a post: its own text, the summary when it has none."""
    return _field(item, "raw_text", "") or _field(item, "summary", "") or ""


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
    sent_text: dict[int, str],
    vec: dict[int, np.ndarray],
    sent_vec: dict[int, np.ndarray],
) -> tuple[dict[int, int], dict[int, int]]:
    """B1 — confirm each candidate against the very post it would hide under.

    Embeddings only pre-select: in war and strike news two DIFFERENT events score the
    same cosine as one event retold. A pair at/above dedup_auto_hide_threshold is hidden
    outright (dedup_auto_hide_in_digest_threshold when both posts are in this digest and
    category, where the hidden one stays linked beside its primary); every other pair
    gets its own verdict from judge_pairs on the raw text. "same" hides. "update" (same
    story, new fact) hides only inside this digest and only when the candidate says no
    more than its primary (_hides_update); against a story already SHOWN it is news, so
    the item stays and is returned in `updates` (candidate -> shown id) for a ↻ link
    back. Fail-open: no verdict keeps the item shown.

    Returns (confirmed mutes, updates)."""
    confirmed: dict[int, int] = {}
    updates: dict[int, int] = {}
    pending: list[tuple[int, int]] = []
    score_of: dict[int, float] = {}
    for mid, pid in muted.items():
        mv = vec.get(mid)
        pv = vec.get(pid)
        if pv is None:
            pv = sent_vec.get(pid)
        score = cosine(mv, pv) if mv is not None and pv is not None else 0.0
        # Across categories the in-digest line would equal the candidate floor
        # (dedup_cross_category_threshold) and no such pair would ever reach the judge.
        same_digest_and_cat = pid in item_by_id and (
            (_field(item_by_id[mid], "category", "other") or "other")
            == (_field(item_by_id[pid], "category", "other") or "other"))
        # A hide without a verdict must not bury a richer post under a one-line primary
        # either: such a pair always goes to the judge.
        primary_richness = (_richness(item_by_id[pid]) if pid in item_by_id
                            else _richness({"raw_text": sent_text.get(pid, "")}))
        buries_richer = _richness(item_by_id[mid]) > primary_richness
        line = (settings.dedup_auto_hide_in_digest_threshold if same_digest_and_cat
                else settings.dedup_auto_hide_threshold)
        score_of[mid] = score
        if score >= line and not buries_richer:
            confirmed[mid] = pid
            log.info("B1: auto-hid item id=%d -> primary id=%d | cosine=%.3f", mid, pid, score)
        else:
            pending.append((mid, pid))

    def _text(iid: int) -> str:
        return _post_text(item_by_id[iid]) if iid in item_by_id else sent_text.get(iid, "")

    verdicts = await judge_pairs([(_text(pid), _text(mid)) for mid, pid in pending])
    rejected: list[tuple[int, int]] = []
    for (mid, pid), verdict in zip(pending, verdicts):
        if verdict == "same" or (verdict == "update" and _hides_update(mid, pid, item_by_id)):
            confirmed[mid] = pid
            log.info("B1: hid item id=%d -> primary id=%d | cosine=%.3f | verdict=%s", mid, pid, score_of[mid], verdict)
            continue
        if verdict == "update" and pid not in item_by_id:
            updates[mid] = pid
            log.info("B1: kept item id=%d — an update to shown id=%d | cosine=%.3f", mid, pid, score_of[mid])
        else:
            log.info("B1: kept item id=%d — verdict=%s against primary id=%d | cosine=%.3f",
                     mid, verdict, pid, score_of[mid])
        rejected.append((mid, pid))

    confirmed.update(await _regroup_rejected(rejected, item_by_id, vec))
    return confirmed, updates


def _hides_update(mid: int, pid: int, item_by_id: dict) -> bool:
    """An update may hide only inside this digest, under a primary that says at least as
    much: _sort_key puts a Telegram post first, so a one-line Telegram primary would
    otherwise bury the RSS article that carries the new figure."""
    return pid in item_by_id and _richness(item_by_id[mid]) <= _richness(item_by_id[pid])


async def _regroup_rejected(
    rejected: list[tuple[int, int]],
    item_by_id: dict,
    vec: dict[int, np.ndarray],
) -> dict[int, int]:
    """Candidates rejected against ONE primary may still be one event between them.

    Union-find chains a cluster transitively (A~B~C), but B1 asks only "same as the
    PRIMARY?". When the primary is the weak link every candidate is rejected and the
    cluster ships whole — prod 2026-09-02: two sources on one downed Ka-27 (cosine
    0.966), both delivered, both compared only with an unrelated primary. So rejected
    candidates of one primary that clear the floor with each other (same category,
    different sources) are judged once more against the best of them."""
    by_primary: dict[int, list[int]] = defaultdict(list)
    for mid, pid in rejected:
        if mid in item_by_id:
            by_primary[pid].append(mid)

    pairs: list[tuple[int, int, float]] = []  # (candidate, survivor, cosine)
    for members in by_primary.values():
        if len(members) < 2:
            continue
        uf = _UnionFind()
        for a in range(len(members)):
            ia = item_by_id[members[a]]
            uf.find(members[a])
            for b in range(a + 1, len(members)):
                ib = item_by_id[members[b]]
                va, vb = vec.get(members[a]), vec.get(members[b])
                if (va is None or vb is None or _field(ia, "source_id") == _field(ib, "source_id")
                        or (_field(ia, "category", "other") or "other") != (_field(ib, "category", "other") or "other")):
                    continue
                if cosine(va, vb) >= settings.dedup_log_floor:
                    uf.union(members[a], members[b])
        comps: dict[int, list[int]] = defaultdict(list)
        for m in members:
            comps[uf.find(m)].append(m)
        for comp in comps.values():
            if len(comp) < 2:
                continue
            survivor = min(comp, key=lambda i: _sort_key(item_by_id[i]))
            survivor_src = _field(item_by_id[survivor], "source_id")
            # Each pair clears the floor on its own: a chain A~B~C must not put C before
            # the judge against an A its vectors never linked it to.
            for m in comp:
                if m == survivor or _field(item_by_id[m], "source_id") == survivor_src:
                    continue
                c = cosine(vec[m], vec[survivor])
                if c >= settings.dedup_log_floor:
                    pairs.append((m, survivor, c))
    if not pairs:
        return {}

    verdicts = await judge_pairs([(_post_text(item_by_id[sid]), _post_text(item_by_id[mid]))
                                  for mid, sid, _c in pairs])
    out: dict[int, int] = {}
    for (mid, sid, c), verdict in zip(pairs, verdicts):
        if verdict == "same" or (verdict == "update" and _hides_update(mid, sid, item_by_id)):
            out[mid] = sid
            log.info("B1: regrouped item id=%d -> primary id=%d | cosine=%.3f | verdict=%s | both were rejected "
                     "against their own primary", mid, sid, c, verdict)
    return out


def _sent_matches(
    cur: list[tuple[int, np.ndarray]], sent_meta: dict, item_by_id: dict,
) -> tuple[list[tuple[int, int, float]], list[tuple[int, int, float]]]:
    """(current id, sent id, cosine) for each current item's best match in the sent pool,
    plus each item's best same-source match below the near-identical line: a follow-up
    candidate, never hidden, only linked back (judged only if the item survives B1).

    Each item is compared with the pool DIRECTLY, never through a chain of current items —
    union-find once muted a "$81K Bitcoin" post at cosine 0.755 under a CLARITY Act one it
    was only linked to via a third post. A post from the SAME source as the shown one is
    that channel's next development of the story (a court ruling, a new death toll), so
    only a near-identical repost of it counts: on 2026-09-18..10-07 such mutes were 42% of
    all, and 26 of 30 checked by hand were new events or new facts. One matrix product,
    since the pool spans every category (~1500 items against ~150)."""
    if not cur or not sent_meta:
        return [], []
    # One length per model (from_blob already drops the others); the majority guards the
    # product against a stray vector instead of trusting whichever item came first.
    dim = Counter(v.shape for _, v in cur).most_common(1)[0][0]
    sids = [sid for sid, (v, _c, _s) in sent_meta.items() if v.shape == dim]
    rows = [(iid, v) for iid, v in cur if v.shape == dim]
    if not sids or not rows:
        return [], []

    def _unit(m: np.ndarray) -> np.ndarray:
        n = np.linalg.norm(m, axis=1, keepdims=True)
        return m / np.where(n == 0, 1, n)

    sims = _unit(np.stack([v for _, v in rows])) @ _unit(np.stack([sent_meta[s][0] for s in sids])).T
    lowest = min(settings.dedup_log_floor, settings.dedup_cross_category_threshold)
    out, follow = [], []
    for r, (iid, _v) in enumerate(rows):
        item = item_by_id[iid]
        cat, src = _field(item, "category", "other") or "other", _field(item, "source_id")
        best: tuple[float, int] | None = None
        best_follow: tuple[float, int] | None = None
        for k in np.nonzero(sims[r] >= lowest)[0]:
            sid, c = sids[k], float(sims[r, k])
            _v, s_cat, s_src = sent_meta[sid]
            if c < _union_floor(cat, s_cat):
                continue
            if s_src == src and c < settings.merge_near_dup_threshold:
                if best_follow is None or c > best_follow[0]:
                    best_follow = (c, sid)
                continue
            if best is None or c > best[0]:
                best = (c, sid)
        if best is not None:
            out.append((iid, best[1], best[0]))
        if best_follow is not None:
            follow.append((iid, best_follow[1], best_follow[0]))
    return out, follow


async def _confirm_follow_ups(follow: list[tuple[int, int, float]], item_by_id: dict,
                              sent_text: dict[int, str]) -> dict[int, int]:
    """A source's new post on a story it already showed is never hidden (see
    _sent_matches), but half of these pairs are a different event in the same words, so
    the ↻ link back is drawn only where the judge reads the same story."""
    if not follow:
        return {}

    verdicts = await judge_pairs([(sent_text.get(sid, ""), _post_text(item_by_id[iid])) for iid, sid, _c in follow])
    out: dict[int, int] = {}
    for (iid, sid, c), verdict in zip(follow, verdicts):
        if verdict in ("same", "update"):
            out[iid] = sid
            log.info("Follow-up: item id=%d moves on its source's shown id=%d | cosine=%.3f | verdict=%s",
                     iid, sid, c, verdict)
    return out


def _with_earlier(item, earlier: tuple[str, str]) -> dict:
    """The item as a dict carrying a ↻ link to the shown post it moves on."""
    out = dict(item) if isinstance(item, dict) else {k: item[k] for k in item.keys()}
    out["_earlier"] = [earlier] + list(out.get("_earlier") or [])
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
    sent_text: dict[int, str] = {}
    sent_link: dict[int, tuple[str, str]] = {}  # id -> (published_at, original_url)
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
            sent_text[row["id"]] = row_get(row, "raw_text", "") or sent_summary[row["id"]]
            sent_link[row["id"]] = (row_get(row, "published_at", "") or "", row_get(row, "original_url", "") or "")

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

    sent_hits, follow = _sent_matches(cur, sent_meta, item_by_id)
    for ida, sid, c in sent_hits:
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

    if not muted and not near_muted and not follow:
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
    confirmed, updates = await _confirm_mutes(muted, item_by_id, sent_text, vec, sent_vec) if muted else ({}, {})
    # A fallback whose own primary is being hidden would re-point through it to a shown
    # story the item was never compared with — skip those.
    retry = {mid: pid for mid, pid in fallback.items() if mid not in confirmed and pid not in confirmed}
    if retry:
        candidates += len(retry)
        confirmed.update((await _confirm_mutes(retry, item_by_id, sent_text, vec, sent_vec))[0])
    confirmed.update(near_muted)
    updates.update(await _confirm_follow_ups(
        [f for f in follow if f[0] not in confirmed and f[0] not in updates], item_by_id, sent_text))

    def _linked(survivors: list) -> list:
        """Each surviving update of a shown story, with a ↻ link back to that post."""
        out = []
        for it in survivors:
            sid = updates.get(_field(it, "id"))
            if sid is not None and sent_link.get(sid, ("", ""))[1]:
                it = _with_earlier(it, sent_link[sid])
            out.append(it)
        return out

    if not confirmed:
        log.info("Cross-source dedup: %d candidate(s) all rejected by LLM, nothing muted | %d update(s) linked back",
                 candidates, len(updates))
        return _linked(items), {}

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
    # An update hidden under a post of this digest hands its ↻ link to that post: the
    # story it continues was still shown before.
    for mid, pid in muted.items():
        if mid in updates and pid not in updates and pid not in muted:
            updates[pid] = updates[mid]
    for mid, pid in muted.items():
        await mark_duplicate(mid, pid)
    survivors = _linked([it for it in items if _field(it, "id") not in muted])
    link_map = await get_duplicate_links([_field(it, "id") for it in survivors])
    log.info("Cross-source dedup: muted %d by verdict or cosine (of %d) + %d near-identical, %d survivor(s) | "
             "%d update(s) linked back", len(muted) - len(near_muted), candidates, len(near_muted), len(survivors),
             sum(1 for it in survivors if _field(it, "id") in updates))
    return survivors, link_map

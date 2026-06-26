"""Within-source merge: collapse a single source's items that describe the same
event into one digest line.

Hybrid strategy: embeddings (reusing the dedup vectors) PRE-FILTER plausibly
related items into candidate clusters, then the LLM decides the real same-event
grouping inside each candidate. Embeddings alone over-merge in high-overlap
domains (different strikes share vocabulary), so the LLM stays the arbiter.
Flip `settings.merge_via_embeddings` off to fall back to the old all-source
`group_by_topic` path.
"""

import logging

from src.config import settings
from src.processor.llm.classifier import group_by_topic, _wants_no_merge
from src.processor.llm.llm_client import is_task_dead
from src.processor.dedup.cross_dedup import cluster_within_source
from src.processor.dedup.embedder import cosine
from src.common.util import row_get

log = logging.getLogger(__name__)

MERGE_MIN_ITEMS = 4


def _items_as_plain(items: list) -> list[dict]:
    return [
        {
            "summary": item["summary"],
            "key_phrase": row_get(item, "key_phrase", ""),
            "original_url": item["original_url"],
            "published_at": item["published_at"],
            "raw_text": item["raw_text"],
            "_item_ids": [item["id"]],
        }
        for item in items
    ]


async def merge_source_items(
    items: list,
    prompt_extra: str | None = None,
    vectors: dict | None = None,
    stats: dict | None = None,
) -> list[dict]:
    if _wants_no_merge(prompt_extra):
        return _items_as_plain(items)
    if settings.merge_via_embeddings and vectors is not None:
        return await _merge_via_embeddings(items, vectors, prompt_extra, stats)
    return await _merge_via_group_by_topic(items, prompt_extra)


def _cluster_summary_fields(cluster: list) -> tuple[str, str]:
    """Most-detailed existing summary of a cluster (fallback when no LLM call)."""
    best = max(cluster, key=lambda it: len((it["summary"] or "")))
    return (best["summary"] or "", row_get(best, "key_phrase", "") or "")


def _build_merged(cluster: list, summary: str, key_phrase: str) -> dict:
    if summary and len(cluster) > 1:
        summary = f"{summary} · merged {len(cluster)}"
    return {
        "summary": summary,
        "key_phrase": key_phrase,
        "original_url": next((it["original_url"] for it in cluster if it["original_url"]), None),
        "published_at": max((it["published_at"] for it in cluster if it["published_at"]), default=None),
        "raw_text": None,
        "_item_ids": [it["id"] for it in cluster],
    }


async def _llm_subgroup(cluster: list, prompt_extra: str | None) -> list[tuple[list, str, str]]:
    """Let the LLM split an embedding candidate cluster into real same-event
    groups (embeddings over-merge in high-overlap domains, so the LLM is the
    arbiter). Returns (items, summary, key_phrase) per resulting group."""
    raw_inputs = [{"id": i, "text": it["summary"] or it["raw_text"] or ""} for i, it in enumerate(cluster)]
    groups = await group_by_topic(raw_inputs, prompt_extra=prompt_extra)
    out = []
    for g in groups:
        sub = [cluster[i] for i in g["ids"]]
        out.append((sub, g["summary"] or "", g.get("key_phrase") or ""))
    return out


async def _merge_via_embeddings(
    items: list,
    vectors: dict,
    prompt_extra: str | None,
    stats: dict | None,
) -> list[dict]:
    if len(items) < 2:
        return _items_as_plain(items)
    # Embeddings only PRE-FILTER plausibly-related items; the LLM decides whether
    # a candidate cluster is actually one event (different strikes share vocabulary
    # and would otherwise be wrongly merged).
    clusters = cluster_within_source(items, vectors, settings.merge_prefilter_threshold)
    out: list[dict] = []
    for cluster in clusters:
        if len(cluster) == 1:
            out.extend(_items_as_plain(cluster))
            continue
        ids = [it["id"] for it in cluster]
        min_cos = min(
            cosine(vectors[ids[i]], vectors[ids[j]])
            for i in range(len(ids)) for j in range(i + 1, len(ids))
        )
        if min_cos >= settings.merge_near_dup_threshold:
            # Near-identical (reposts/paraphrases): safe to merge without the LLM.
            log.info("MERGE-CANDIDATE min_cos=%.3f size=%d -> near-dup auto-merge", min_cos, len(cluster))
            summary, key_phrase = _cluster_summary_fields(cluster)
            out.append(_build_merged(cluster, summary, key_phrase))
            if stats is not None:
                stats["near_dup"] += 1
            continue
        try:
            subgroups = await _llm_subgroup(cluster, prompt_extra)
        except Exception as exc:
            log.warning("LLM subgrouping failed, keeping items separate: %s", exc)
            out.extend(_items_as_plain(cluster))
            continue
        merged_sizes = [len(sub) for sub, _, _ in subgroups if len(sub) > 1]
        log.info("MERGE-CANDIDATE min_cos=%.3f size=%d -> LLM verdict: %s",
                 min_cos, len(cluster),
                 f"merged {merged_sizes}" if merged_sizes else "split (all separate)")
        if stats is not None:
            stats["llm"] += 1
        for sub, summ, kp in subgroups:
            if len(sub) == 1:
                out.extend(_items_as_plain(sub))
                continue
            if not summ:
                summ, kp = _cluster_summary_fields(sub)
            out.append(_build_merged(sub, summ, kp))
            if stats is not None:
                stats["clusters"] += 1
    return out


async def _merge_via_group_by_topic(items: list, prompt_extra: str | None = None) -> list[dict]:
    if len(items) < MERGE_MIN_ITEMS:
        return _items_as_plain(items)

    if is_task_dead("group"):
        log.info("Skipping group_by_topic for source: all group-task models quota dead, returning items as-is")
        return _items_as_plain(items)

    raw_inputs = [{"id": i, "text": item["summary"] or item["raw_text"] or ""} for i, item in enumerate(items)]
    try:
        groups = await group_by_topic(raw_inputs, prompt_extra=prompt_extra)
        merged = []
        for g in groups:
            group_items = [items[i] for i in g["ids"]]
            url = next((x["original_url"] for x in group_items if x["original_url"]), None)
            pub = max(
                (x["published_at"] for x in group_items if x["published_at"]),
                default=None,
            )
            summary = g["summary"] or group_items[0]["summary"] or ""
            if not summary:
                raw_fallback = (group_items[0]["raw_text"] or "")[:80].split("\n")[0]
                summary = raw_fallback
            n = len(g["ids"])
            if n > 1:
                summary = f"{summary} · merged {n}"
            merged.append({
                "summary": summary,
                "key_phrase": g.get("key_phrase") or "",
                "original_url": url,
                "published_at": pub,
                "raw_text": None,
                "_item_ids": [gi["id"] for gi in group_items],
            })
        return merged
    except Exception as exc:
        log.warning("Topic merging failed, using original items: %s", exc)
        return _items_as_plain(items)

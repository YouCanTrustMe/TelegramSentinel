"""Classifier pure helpers: Ukrainian detection, prompt-extra keyword switches,
media-prefix stripping and the truncation marker."""
import src.db.models as models
import src.processor.llm.classifier as classifier
import src.processor.dedup.cross_dedup as cross_dedup
from src.processor.llm.classifier import (
    ClassificationResult,
    _looks_ukrainian,
    _mark_big,
    _strip_media_prefix,
    _wants_no_filter,
    _wants_no_merge,
    _wants_no_translate,
    classify_pending_items,
)


def test_looks_ukrainian():
    assert _looks_ukrainian("Привіт світ усім людям")
    assert not _looks_ukrainian("Bitcoin price drops today")
    assert _looks_ukrainian("")          # empty -> treated as fine
    assert _looks_ukrainian("Hi")        # <4 letters -> not flagged


def test_proper_nouns_in_ukrainian_still_pass():
    assert _looks_ukrainian("Bitcoin впав на 8% після рішення ФРС")


def test_wants_no_merge():
    assert _wants_no_merge("please no merge here")
    assert _wants_no_merge("не об'єднувати пости")
    assert not _wants_no_merge("focus on numbers")
    assert not _wants_no_merge(None)


def test_wants_no_filter():
    assert _wants_no_filter("bypass filter for this source")
    assert not _wants_no_filter("keep proper nouns")


def test_wants_no_translate():
    assert _wants_no_translate("no translation, keep original language")
    assert not _wants_no_translate(None)


def test_strip_media_prefix():
    assert _strip_media_prefix("[Photo] hello world") == "hello world"
    assert _strip_media_prefix("[Video] clip") == "clip"
    assert _strip_media_prefix("plain text") == "plain text"


def test_mark_big_appends_only_when_truncated():
    long_src = "x" * 2000
    assert _mark_big("summary", long_src, cap=1500).endswith("…")
    assert _mark_big("summary", "short", cap=1500) == "summary"
    assert _mark_big("", long_src, cap=1500) == ""          # empty summary untouched
    assert _mark_big("ends …", long_src, cap=1500) == "ends …"  # no double marker


async def test_classify_pending_embeds_freshly_classified(monkeypatch):
    """Newly summarised items are embedded in the same pass, so the digest finds
    their vectors cached instead of embedding everything at once."""
    long_text = "Реальна новина про важливу подію " * 4  # > _TRIVIAL_MAX_LEN
    items = [
        {"id": 1, "summary": "", "raw_text": long_text},
        {"id": 2, "summary": "", "raw_text": long_text},
    ]
    embedded: dict = {}

    async def fake_get_unsent_items(*a, **k):
        return items

    async def fake_update(item_id, summary, key_phrase):
        pass

    async def fake_classify_batch(payload):
        return {row["id"]: ClassificationResult(summary=f"summary {row['id']}") for row in payload}

    async def fake_ensure_embeddings(passed):
        embedded["ids"] = {row["id"] for row in passed}
        return {}

    monkeypatch.setattr(models, "get_unsent_items", fake_get_unsent_items)
    monkeypatch.setattr(models, "update_item_classification", fake_update)
    monkeypatch.setattr(classifier, "classify_batch", fake_classify_batch)
    monkeypatch.setattr(classifier, "is_task_dead", lambda *_: False)
    monkeypatch.setattr(classifier.settings, "dedup_enabled", True)
    monkeypatch.setattr(cross_dedup, "ensure_embeddings", fake_ensure_embeddings)

    await classify_pending_items(limit=10)

    assert embedded["ids"] == {1, 2}


def test_check_blocked_filters_blocks_only_at_threshold_8(monkeypatch):
    """Threshold raised 7→8: a confidence-7 match is now KEPT, only >=8 blocks."""
    import asyncio
    items = [
        {"id": 1, "text": "buy now, register via link!", "source": "s", "category": "feed"},
        {"id": 2, "text": "ordinary news item", "source": "s", "category": "feed"},
    ]
    rules = ["advertising and promo posts"]

    async def fake_llm(messages, max_retries=3, task="filter"):
        return {"blocked": [
            {"id": 1, "rule": 0, "confidence": 8},
            {"id": 2, "rule": 0, "confidence": 7},
        ]}

    monkeypatch.setattr(classifier, "llm_json", fake_llm)
    out = asyncio.run(classifier.check_blocked_filters(items, rules))
    assert 1 in out and 2 not in out


def test_group_by_topic_drops_empty_and_phantom_ids(monkeypatch):
    """A malformed LLM partition — an empty `ids` group plus a hallucinated id —
    must not crash and must keep every real input id covered exactly once.
    Regression for the empty-cluster ValueError that killed two prod digests."""
    import asyncio
    items = [{"id": 0, "text": "a"}, {"id": 1, "text": "b"}, {"id": 2, "text": "c"}]

    async def fake_llm(messages, max_retries=3, task="group"):
        return {"groups": [
            {"ids": [], "summary": "", "key_phrase": ""},          # empty -> dropped
            {"ids": [0, 9], "summary": "x", "key_phrase": "k"},    # 9 is phantom -> filtered
            {"ids": [1], "summary": "y", "key_phrase": "k"},
            # id 2 omitted by the model -> reconciled as a singleton
        ]}

    async def identity(summary, key_phrase):
        return summary, key_phrase

    monkeypatch.setattr(classifier, "llm_json", fake_llm)
    monkeypatch.setattr(classifier, "_ensure_ukrainian", identity)
    groups = asyncio.run(classifier.group_by_topic(items))

    covered = sorted(i for g in groups for i in g["ids"])
    assert covered == [0, 1, 2]                 # every real id, none dropped, no phantom 9
    assert all(g["ids"] for g in groups)        # no empty group survived


def test_check_blocked_filters_drops_out_of_scope_block(monkeypatch):
    """A rule scoped to one category must not block an item from another, even if
    the model returns it (guards against the over-blocking we saw in prod)."""
    import asyncio
    items = [
        {"id": 1, "text": "feed item", "source": "s", "category": "feed"},
        {"id": 2, "text": "crypto item", "source": "s", "category": "crypto"},
    ]
    rules = ["local traffic accidents"]
    scopes = [{"feed"}]  # rule only applies to the feed category

    async def fake_llm(messages, max_retries=3, task="filter"):
        return {"blocked": [{"id": 2, "rule": 0, "confidence": 10}]}  # crypto, out of scope

    monkeypatch.setattr(classifier, "llm_json", fake_llm)
    out = asyncio.run(classifier.check_blocked_filters(items, rules, scopes))
    assert out == {}


def test_check_blocked_filters_splits_into_small_chunks(monkeypatch):
    """Recall of the semantic filter falls off with batch size — measured on prod feed
    items, a 25-item call caught 3 of 6 air-raid posts where a 10-item call caught 6.
    Every item must still be judged exactly once across the chunks."""
    import asyncio
    items = [{"id": i, "text": f"item {i}", "source": "s", "category": "feed"}
             for i in range(1, 26)]
    seen: list[int] = []

    async def fake_llm(messages, max_retries=3, task="filter"):
        body = messages[-1]["content"]
        ids = [int(line.split(" ")[0]) for line in body.split("Items:\n")[1].splitlines()]
        seen.extend(ids)
        return {"blocked": [{"id": ids[0], "rule": 0, "confidence": 10}]}

    monkeypatch.setattr(classifier, "llm_json", fake_llm)
    monkeypatch.setattr(classifier, "_FILTER_CHUNK", 10)
    out = asyncio.run(classifier.check_blocked_filters(items, ["a rule"]))

    assert sorted(seen) == [i["id"] for i in items]   # every item judged once
    assert len(out) == 3                              # one block per chunk of 10/10/5


def test_check_blocked_filters_survives_one_failing_chunk(monkeypatch):
    """A chunk that errors must pass its own items through unfiltered, not abort the
    filter for the whole digest (fail-open, as everywhere else in the pipeline)."""
    import asyncio
    items = [{"id": i, "text": f"item {i}", "source": "s", "category": "feed"}
             for i in range(1, 21)]

    async def fake_llm(messages, max_retries=3, task="filter"):
        body = messages[-1]["content"]
        first = int(body.split("Items:\n")[1].splitlines()[0].split(" ")[0])
        if first == 1:
            raise RuntimeError("provider exploded")
        return {"blocked": [{"id": first, "rule": 0, "confidence": 10}]}

    monkeypatch.setattr(classifier, "llm_json", fake_llm)
    monkeypatch.setattr(classifier, "_FILTER_CHUNK", 10)
    out = asyncio.run(classifier.check_blocked_filters(items, ["a rule"]))

    assert out == {11: "a rule"}


def test_check_blocked_filters_works_across_separate_event_loops(monkeypatch):
    """The concurrency gate must not be a module-level Semaphore: it would bind to the
    first loop that contends on it and raise in every later one, which — behind
    return_exceptions — degrades silently into a filter that blocks nothing."""
    import asyncio
    items = [{"id": i, "text": f"item {i}", "source": "s", "category": "feed"}
             for i in range(1, 61)]  # 6 chunks > the concurrency limit

    async def fake_llm(messages, max_retries=3, task="filter"):
        first = int(messages[-1]["content"].split("Items:\n")[1].splitlines()[0].split(" ")[0])
        await asyncio.sleep(0)
        return {"blocked": [{"id": first, "rule": 0, "confidence": 10}]}

    monkeypatch.setattr(classifier, "llm_json", fake_llm)
    first_run = asyncio.run(classifier.check_blocked_filters(items, ["a rule"]))
    second_run = asyncio.run(classifier.check_blocked_filters(items, ["a rule"]))

    assert first_run == second_run
    assert len(first_run) == 6
def test_literal_rule_blocks_without_calling_the_model(monkeypatch):
    """A "=" rule is matched in code: the air-raid all-clears a channel posts all day
    are identical strings, and the semantic filter caught them only about half the time."""
    import asyncio
    items = [
        {"id": 1, "text": "🟢 Хмельницький район - ВІДБІЙ ТРИВОГИ!", "source": "s", "category": "feed"},
        {"id": 2, "text": "Рада переїхала в підвал через тривоги", "source": "s", "category": "feed"},
    ]
    rules = ["= відбій тривоги"]
    called = {"n": 0}

    async def fake_llm(messages, max_retries=3, task="filter"):
        called["n"] += 1
        return {"blocked": []}

    monkeypatch.setattr(classifier, "llm_json", fake_llm)
    out = asyncio.run(classifier.check_blocked_filters(items, rules))

    assert out == {1: "= відбій тривоги"}
    assert called["n"] == 0  # the only rule was literal, so nothing to ask


def test_literal_rule_normalizes_whitespace_and_case(monkeypatch):
    import asyncio
    items = [{"id": 1, "text": "🟢 Шепетівський  район  -\nвідбій\tтривоги!", "source": "s", "category": "feed"}]

    async def fake_llm(messages, max_retries=3, task="filter"):
        raise AssertionError("literal rules must not reach the model")

    monkeypatch.setattr(classifier, "llm_json", fake_llm)
    assert asyncio.run(classifier.check_blocked_filters(items, ["= ВІДБІЙ ТРИВОГИ"])) == {1: "= ВІДБІЙ ТРИВОГИ"}


def test_literal_rule_respects_its_category_scope(monkeypatch):
    import asyncio
    items = [{"id": 1, "text": "відбій тривоги", "source": "s", "category": "crypto"}]

    async def fake_llm(messages, max_retries=3, task="filter"):
        return {"blocked": []}

    monkeypatch.setattr(classifier, "llm_json", fake_llm)
    out = asyncio.run(classifier.check_blocked_filters(items, ["= відбій тривоги"], [{"feed"}]))

    assert out == {}


def test_semantic_rules_still_run_for_items_no_literal_rule_caught(monkeypatch):
    """Mixed rule set: the literal one short-circuits its own item, the rest of the
    batch still goes to the model, and the literal rule is not shown to it."""
    import asyncio
    items = [
        {"id": 1, "text": "відбій тривоги", "source": "s", "category": "feed"},
        {"id": 2, "text": "buy now via this referral link", "source": "s", "category": "feed"},
    ]
    rules = ["= відбій тривоги", "advertising and promo posts"]
    seen: list[str] = []

    async def fake_llm(messages, max_retries=3, task="filter"):
        body = messages[-1]["content"]
        seen.append(body)
        return {"blocked": [{"id": 2, "rule": 1, "confidence": 9}]}

    monkeypatch.setattr(classifier, "llm_json", fake_llm)
    out = asyncio.run(classifier.check_blocked_filters(items, rules))

    assert out == {1: "= відбій тривоги", 2: "advertising and promo posts"}
    assert len(seen) == 1
    assert "відбій" not in seen[0]           # neither the literal rule nor its item
    assert "1. advertising" in seen[0]       # rule indices stay aligned with `rules`


def test_block_is_dropped_when_the_chunk_never_saw_that_rule(monkeypatch):
    """The numbered rule list has gaps — out-of-scope and literal rules are left out —
    so an index the chunk was never shown is drift. Blocking on it would record the
    wrong rule, and a literal rule carries no scope for the out_of_scope guard."""
    import asyncio
    items = [{"id": 1, "text": "ordinary news", "source": "s", "category": "feed"}]
    rules = ["= відбій тривоги", "advertising and promo posts"]

    async def fake_llm(messages, max_retries=3, task="filter"):
        return {"blocked": [{"id": 1, "rule": 0, "confidence": 10}]}  # the literal rule

    monkeypatch.setattr(classifier, "llm_json", fake_llm)
    assert asyncio.run(classifier.check_blocked_filters(items, rules)) == {}


def test_verdict_for_an_item_outside_the_chunk_is_ignored(monkeypatch):
    """Item ids are consecutive and chunks are adjacent slices, so one drifted digit
    names a real item of the next chunk — which would be dropped from the digest under
    a rule it never matched."""
    import asyncio
    items = [{"id": i, "text": f"item {i}", "source": "s", "category": "feed"}
             for i in (11, 12, 13)]

    async def fake_llm(messages, max_retries=3, task="filter"):
        return {"blocked": [{"id": 21, "rule": 0, "confidence": 10}]}  # not in this batch

    monkeypatch.setattr(classifier, "llm_json", fake_llm)
    assert asyncio.run(classifier.check_blocked_filters(items, ["a rule"])) == {}


def test_a_bare_equals_rule_matches_nothing_and_never_reaches_the_model(monkeypatch):
    """"=" with no pattern would otherwise be handed to the model as an empty semantic
    rule, which matches anything."""
    import asyncio
    items = [{"id": 1, "text": "ordinary news", "source": "s", "category": "feed"}]
    seen: list[str] = []

    async def fake_llm(messages, max_retries=3, task="filter"):
        seen.append(messages[-1]["content"])
        return {"blocked": []}

    monkeypatch.setattr(classifier, "llm_json", fake_llm)
    out = asyncio.run(classifier.check_blocked_filters(items, ["=", "advertising posts"]))

    assert out == {}
    assert seen and "0. =" not in seen[0]
    assert "1. advertising posts" in seen[0]

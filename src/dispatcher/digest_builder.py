import asyncio
import logging
import re
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from html import escape
from zoneinfo import ZoneInfo

from src.config import settings
from src.db.models import get_app_setting, get_blocked_words, get_categories, get_silent_sources, get_unsent_items, get_word_category_map, log_digest, mark_blocked, mark_sent, set_app_setting, update_item_classification
from src.dispatcher.sender import delete_message, edit_message, pin_message, send_message, unpin_message
from src.processor.llm.classifier import ClassificationResult, classify, check_blocked_filters, pick_war_reports, _wants_no_merge, _wants_no_filter
from src.processor.dedup.cross_dedup import deduplicate, ensure_embeddings
from src.processor.llm.llm_client import format_llm_stats, reset_llm_stats, is_task_dead
from src.processor.dedup.merge import MERGE_MIN_ITEMS, merge_source_items
from src.common.media import MEDIA_LABEL as _MEDIA_LABEL, is_media_placeholder
from src.common.util import needs_summary, row_get, source_link

log = logging.getLogger(__name__)

_digest_lock = asyncio.Lock()
_TELEGRAM_LIMIT = 4000
# A blockquote is indivisible once built, so cap it under Telegram's 4096 limit
# and split a source across several blocks. Counting raw length is conservative.
_MAX_BLOCK_LEN = 3800
_MAX_ITEMS_PER_SOURCE = 50
_DEFER_MAX_DAYS = 3
# Build time scales with the item count (embeddings + LLM calls per item), so the
# threshold does too: an absolute one fires on SIZE rather than on sickness (116
# healthy items took 101.6s = 0.88s/item, while the regression that once ran to
# ~9 min was 2.6s/item). The floor keeps a handful of items from tripping it.
_SLOW_DIGEST_PER_ITEM = 1.5
# 3 minutes: the only slow digest in 60 (2026-09-29, 124s for 72 items) was five Mistral
# rate limits that cleared on their own and the digest went out whole — not worth a ping.
_SLOW_DIGEST_FLOOR = 180.0


def _slow_digest_threshold(item_count: int) -> float:
    return max(_SLOW_DIGEST_FLOOR, _SLOW_DIGEST_PER_ITEM * item_count)


def _slow_digest_warning(elapsed_s: float, item_count: int,
                         threshold_s: float | None = None) -> str | None:
    """Return an admin-facing warning string when a digest took too long for its
    size, else None."""
    if threshold_s is None:
        threshold_s = _slow_digest_threshold(item_count)
    if elapsed_s <= threshold_s:
        return None
    per_item = elapsed_s / item_count if item_count else elapsed_s
    return (f"Slow digest: build took {elapsed_s:.0f}s for {item_count} items "
            f"({per_item:.2f}s/item, > {threshold_s:.0f}s threshold)")


# Shortest clickable anchor we render: 1-3 char key phrases (Fed, РФ, BTC) are
# hard to tap on mobile, so the anchor span is grown by whole words until it
# reaches this length.
_MIN_ANCHOR_CHARS = 4


def _grow_anchor(text: str, idx: int, end: int) -> int:
    """Extend the anchor span [idx:end] rightwards so the clickable text is long
    enough to tap on mobile: complete the current word, then pull in following
    words until the span is at least _MIN_ANCHOR_CHARS long (or text runs out)."""
    n = len(text)
    while end < n and _WORD_CHAR_RE.match(text[end]):
        end += 1
    while end - idx < _MIN_ANCHOR_CHARS and end < n:
        while end < n and not _WORD_CHAR_RE.match(text[end]):
            end += 1
        while end < n and _WORD_CHAR_RE.match(text[end]):
            end += 1
    return end


def _anchor_link(text: str, idx: int, end: int, url: str) -> str:
    """Render `text` with the span [idx:end] — grown to a tappable length — wrapped
    in a link to `url`; the text before and after the span stays plain, with the
    sentence's own spacing and punctuation untouched (re-joining the three parts
    with a space put one in front of every comma following an anchor)."""
    end = _grow_anchor(text, idx, end)
    span = text[idx:end]
    idx += len(span) - len(span.lstrip())
    end -= len(span) - len(span.rstrip())
    link = f'<a href="{escape(url, quote=True)}">{escape(text[idx:end])}</a>'
    return f"{escape(text[:idx])}{link}{escape(text[end:])}"


# The model's key_phrase reaches the summary intact only about two thirds of the
# time (measured on 60 prod items, 2026-09-01): the rest come back re-worded or
# re-inflected ("вибухи в Полтаві" for "У Полтаві чутно вибухи"), and the old
# fallback then anchored the link on the summary's FIRST word — usually a
# preposition or the leading entity, never the point of the news. So a miss is
# resolved in two more steps before that fallback is reached.
_ANCHOR_STEM_CHARS = 4
# Ukrainian function words: never the whole point of a headline, so they are
# skipped both when matching a re-worded phrase and when picking a fallback span.
_ANCHOR_STOPWORDS = frozenset(
    "і й та а але в у на з із зі до від для про за під над при по о об без крізь через "
    "як що щоб бо це цей ця ці той та те тих його її їх він вона воно вони ми ви я ти "
    "не ні також ще вже лише тільки між серед після перед".split()
)
# Ukrainian words carry an internal apostrophe (Прем'єр, П'ять) and hyphen
# (Івано-Франківськ, 16-поверховий); a bare \w+ splits them, and an anchor then
# starts mid-word — "Прем'<a>єр підписав</a>".
_WORD_RE = re.compile(r"\w+(?:[-’'ʼ‘]\w+)*", re.UNICODE)
_WORD_CHAR_RE = re.compile(r"[\w’'ʼ‘]", re.UNICODE)


def _stem(word: str) -> str:
    """Crude inflection-insensitive key: Ukrainian endings vary (Полтаві/Полтава,
    вибухи/вибух), so compare on a leading slice rather than the whole word."""
    return word.lower()[:_ANCHOR_STEM_CHARS]


def _fallback_anchor(text: str) -> tuple[int, int]:
    """Anchor span for a summary whose key_phrase matched nothing: the first word
    that carries meaning — skipping leading function words and the opening
    entity run, since the summary is instructed to start with the entity."""
    words = list(_WORD_RE.finditer(text))
    if not words:
        return 0, len(text.split(" ", 1)[0])
    meaningful = [m for m in words if m.group().lower() not in _ANCHOR_STOPWORDS]
    if not meaningful:
        return words[0].start(), words[0].end()
    # Step past the opening capitalised run (a name, org or place) when there is
    # anything after it; a summary that is only a name keeps the name.
    i = 0
    while i < len(meaningful) - 1 and meaningful[i].group()[:1].isupper():
        i += 1
    return meaningful[i].start(), meaningful[i].end()


def _resolve_anchor(text: str, key_phrase: str) -> tuple[int, int]:
    """Locate the span of `text` to hang the link on. Exact match first, then a
    stem match that survives re-wording and inflection, then the fallback span."""
    if key_phrase:
        idx = text.lower().find(key_phrase.lower())
        if idx != -1:
            return idx, idx + len(key_phrase)
        wanted = {
            _stem(m.group())
            for m in _WORD_RE.finditer(key_phrase)
            if m.group().lower() not in _ANCHOR_STOPWORDS and len(m.group()) >= 3
        }
        if wanted:
            hits = [m for m in _WORD_RE.finditer(text) if _stem(m.group()) in wanted]
            if hits:
                # Keep the span tight: a phrase scattered across the whole summary
                # is not one anchor, so fall back to its longest single word.
                span_words = sum(1 for m in _WORD_RE.finditer(text) if hits[0].start() <= m.start() <= hits[-1].start())
                if span_words <= len(wanted) + 1:
                    return hits[0].start(), hits[-1].end()
                best = max(hits, key=lambda m: len(m.group()))
                return best.start(), best.end()
    return _fallback_anchor(text)


def _progress_bar(done: int, total: int, width: int = 8) -> str:
    filled = round(width * done / total) if total else 0
    return "▓" * filled + "░" * (width - filled)


def _get_tz() -> ZoneInfo:
    return ZoneInfo(settings.digest_timezone)


def _ids_of(item) -> list[int]:
    """Item id(s) a rendered line stands for: merged groups carry the ids they
    collapsed, un-merged rows carry their own."""
    keys = item.keys()
    if "_item_ids" in keys:
        return list(item["_item_ids"])
    if "id" in keys:
        return [item["id"]]
    return []


def _parse_published(published_at) -> datetime | None:
    if not published_at:
        return None
    try:
        dt = datetime.fromisoformat(published_at)
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _local_hhmm(published_at) -> str:
    dt = _parse_published(published_at)
    return f"{dt.astimezone(_get_tz()):%H:%M}" if dt else ""


# A line cannot be split across blocks, so a story reposted all night links only
# its most recent earlier posts and counts the rest.
_EARLIER_MAX_LINKS = 4


# An earlier post at least this much older than the line's own predates the previous
# digest (slots are under 12h apart; dedup links an update back up to
# dedup_window_hours), so it carries its date and is never cut from the line.
_EARLIER_DATED_AFTER = timedelta(hours=12)


def _is_dated(published_at, line_published_at) -> bool:
    dt, own = _parse_published(published_at), _parse_published(line_published_at)
    return dt is not None and own is not None and own - dt >= _EARLIER_DATED_AFTER


def _earlier_label(published_at, line_published_at) -> str:
    dt = _parse_published(published_at)
    if dt is None:
        return "→"
    local = dt.astimezone(_get_tz())
    return f"{local:%d.%m %H:%M}" if _is_dated(published_at, line_published_at) else f"{local:%H:%M}"


def _earlier_line(item) -> str:
    """Second line of a merged story or of an update to a story shown before: the
    earlier posts, each reachable by its time. Empty for an ordinary item."""
    earlier = [(pub, url) for pub, url in (row_get(item, "_earlier") or []) if url]
    if not earlier:
        return ""
    own = row_get(item, "published_at")
    shown = earlier[-_EARLIER_MAX_LINKS:]
    # The oldest link may be the story shown in an earlier digest: it outranks a repost.
    if len(earlier) > len(shown) and _is_dated(earlier[0][0], own):
        shown = [earlier[0]] + earlier[-(_EARLIER_MAX_LINKS - 1):]
    links = [f'<a href="{escape(url, quote=True)}">{_earlier_label(pub, own)}</a>' for pub, url in shown]
    more = f"+{len(earlier) - len(shown)} · " if len(earlier) > len(shown) else ""
    return f"<i>↻ earlier: {more}{', '.join(links)}</i>"


def _format_item(item: dict, dup_links: dict[int, list[tuple[str, str]]] | None = None) -> str:
    """Render an item line, appending clickable source links for any cross-source
    duplicates muted under it, and the earlier updates of a merged story below it."""
    line = _format_item_base(item)
    if not line:
        return line
    links = []
    if dup_links:
        for iid in _ids_of(item):
            for name, url in dup_links.get(iid, []):
                if url:
                    links.append(f'<a href="{escape(url, quote=True)}">{escape(name)}</a>')
    if links:
        # Italic including the brackets: the trailing source names are provenance, not
        # part of the headline, and the slant separates them at a glance from the link
        # the summary itself carries.
        line = f"{line} <i>({', '.join(links)})</i>"
    via = row_get(item, "_via")
    if via:
        # A block mixing sources (the overnight war block) names each line's channel.
        line = f"{line} <i>· {escape(via)}</i>"
    earlier = _earlier_line(item)
    return f"{line}\n{earlier}" if earlier else line


def _format_item_base(item: dict) -> str:
    url = item["original_url"] or ""
    summary_text = item["summary"] or ""
    if not summary_text:
        raw = item["raw_text"] or ""
        summary_text = raw[:60].split("\n")[0]

    # A media chip ("📷 Photo") is a label, not a sentence: the whole chip is the
    # link, so it stays tappable instead of leaving the emoji outside it.
    is_media_label = summary_text in _MEDIA_LABEL
    summary_text = _MEDIA_LABEL.get(summary_text, summary_text)

    summary = escape(summary_text)
    # Italic HH:MM, the same typeface the digest chrome uses for its service bits,
    # so the column reads as a timeline instead of a second kind of content.
    hhmm = _local_hhmm(item["published_at"])
    stamp = f"<i>{hhmm}</i>" if hhmm else ""

    suffix = ""

    prefix = f"{stamp}  " if stamp else ""
    key_phrase = (row_get(item, "key_phrase", "") or "").strip()
    if url and summary_text.strip():
        rest_text = summary_text.strip()
        idx, end = (0, len(rest_text)) if is_media_label else _resolve_anchor(rest_text, key_phrase)
        return f'{prefix}{_anchor_link(rest_text, idx, end, url)}{suffix}'
    if url:
        escaped_url = escape(url, quote=True)
        return f'{prefix}<a href="{escaped_url}">→</a>{suffix}'
    if summary_text.strip():
        return f"{prefix}{summary}{suffix}"
    return ""


def _source_blocks(
    source_name: str,
    source_items: list,
    dup_links: dict[int, list[tuple[str, str]]] | None = None,
) -> list[tuple[str, list[int]]]:
    """Render a source's items into one or more expandable blockquotes, each
    under _MAX_BLOCK_LEN, paired with the item ids they render."""
    rendered: list[tuple[str, list[int]]] = []
    for item in source_items:
        line = _format_item(item, dup_links)
        if line:
            rendered.append((line, _ids_of(item)))
    if not rendered:
        return []

    header = f"<b>{escape(source_name)}</b>"

    def _wrap(lines: list[str]) -> str:
        return "<blockquote expandable>" + "\n".join([header] + lines) + "</blockquote>"

    blocks: list[tuple[str, list[int]]] = []
    cur_lines: list[str] = []
    cur_ids: list[int] = []
    for line, ids in rendered:
        if cur_lines and len(_wrap(cur_lines + [line])) > _MAX_BLOCK_LEN:
            blocks.append((_wrap(cur_lines), cur_ids))
            cur_lines, cur_ids = [line], list(ids)
        else:
            cur_lines.append(line)
            cur_ids = cur_ids + list(ids)
    if cur_lines:
        blocks.append((_wrap(cur_lines), cur_ids))
    return blocks


# Telegram has no font sizes, so a category header separates itself by texture
# instead: a rule plus letter-spaced caps, which nothing else in the digest uses.
_CATEGORY_RULE = "━" * 18


def _spaced_caps(text: str) -> str:
    return " ".join(text.upper())


def _build_digest_text(
    cat_meta: dict,
    blocked_items: list | None = None,
    dup_links: dict[int, list[tuple[str, str]]] | None = None,
) -> list[tuple[str, list[int]]]:
    """Build the digest body as (text, item_ids) segments so delivery can be
    confirmed per message. Headers and already-marked blocked items carry no ids.
    Per-digest chrome (header / part marker / footer) is added by
    _decorate_messages once the body has been split into messages."""
    segments: list[tuple[str, list[int]]] = []

    for cat_name, data in cat_meta.items():
        sources = data["sources"]
        war = data.get("war_block")
        if not any(sources.values()) and not war:
            continue

        segments.append((
            f"\n<b>{_CATEGORY_RULE}</b>\n<b>{data['emoji']}  {escape(_spaced_caps(cat_name))}</b>",
            [],
        ))

        if war:
            header = f"{war.get('title', _WAR_BLOCK_TITLE)} · {len(war['items'])}"
            for block_text, block_ids in _source_blocks(header, war["items"], dup_links):
                segments.append((block_text, block_ids))

        for source_name, source_items in sources.items():
            if not source_items:
                continue
            for block_text, block_ids in _source_blocks(source_name, source_items, dup_links):
                segments.append((block_text, block_ids))

    if blocked_items:
        segments.append(("\n<b>🚫 Filtered</b>", []))
        filtered_by_word: dict[str, list] = defaultdict(list)
        for item in blocked_items:
            rule = item.get("blocked_by") or "?"
            # Keyed by the full rule, so two rules that open alike keep their own blocks.
            filtered_by_word[_LITERAL_FILTER_TITLE if rule.lstrip().startswith("=") else rule].append(item)
        for word, word_items in filtered_by_word.items():
            for block_text, _ in _source_blocks(_filter_title(word), word_items):
                segments.append((block_text, []))

    return segments


_LITERAL_FILTER_TITLE = "= exact phrases"
_FILTER_TITLE_MAX = 45
# Words a cut title must not end on (the rules are written in Ukrainian, hence the list).
# A list, not a length rule: acronyms like AI or the army's, and words like "war", are
# short and carry the meaning.
_TITLE_DANGLING = frozenset(
    "на в у з із зі до для без про по від та і й або чи що як за при під над через між "
    "to of in on for and or the a an with by at from".split()
)


def _filter_title(rule: str) -> str:
    """Header of a Filtered block. A semantic rule is a paragraph written for the model —
    its first clause names it, the rest (examples, the "do NOT block" clause) is noise in a
    digest. Literal `= phrase` rules each catch a handful of alerts, so they share one
    block; which phrase hid an item stays in items.blocked_reason."""
    rule = " ".join(rule.split())
    if rule.startswith("="):
        return _LITERAL_FILTER_TITLE
    rule = rule.lstrip(":—– ")  # a rule opening with its separator would split to an empty head
    head = re.split(r":| — | – ", rule, maxsplit=1)[0].strip()
    if len(head) > _FILTER_TITLE_MAX:
        words = head[:_FILTER_TITLE_MAX].rsplit(" ", 1)[0].split()
        while len(words) > 1 and (words[-1].strip(",;").lower() in _TITLE_DANGLING or words[-1].isdigit()):
            words.pop()
        head = " ".join(words).rstrip(",;")
    return head if head == rule else f"{head}…"


def _quiet_source_url(row) -> str | None:
    return source_link(row["type"], row["url"])


async def _build_silent_block() -> str:
    sources = await get_silent_sources(120)
    if not sources:
        return ""
    lines = ["<b>💤 Quiet sources</b> (5+ days without new items)"]
    for row in sources:
        hours = row["hours_silent"]
        age = f"{hours // 24}d" if hours is not None else "never"
        name = escape(row["name"])
        url = _quiet_source_url(row)
        label = f'<a href="{escape(url, quote=True)}">{name}</a>' if url else name
        lines.append(f"• {label} [{row['type']}] — {age}")
    return "<blockquote expandable>" + "\n".join(lines) + "</blockquote>"


def _digest_number(now: datetime) -> int:
    """Issue number = day of the year: unique per day and needs no stored counter."""
    return now.timetuple().tm_yday


def _category_tags(cat_meta: dict) -> str:
    return " · ".join(
        f"{data['emoji']} {escape(name)}"
        for name, data in cat_meta.items()
        if any(data["sources"].values()) or data.get("war_block")
    )


_WAR_BLOCK_TITLE = "🌙 War overnight"
# Strikes on cities are daily now; the rest of the day folds them too, under a plainer
# title, so one expandable line replaces a screen of them without losing any.
_WAR_BLOCK_TITLE_DAY = "⚔️ War"


def _is_morning(now: datetime) -> bool:
    """A digest built in the local morning window: it puts the heavy categories last and
    titles its war block "overnight". Deliberately not "the first digest of the day" from
    digest_log: a manual /digest at 08:00, or a send that failed and retried, would then
    take that from the 09:45 one that carries the night. The cost is that a second
    morning digest gets the same title for a few hours."""
    return settings.morning_from_hour <= now.hour < settings.morning_until_hour


def _order_for_morning(cat_meta: dict) -> dict:
    """Move the heavy categories to the end of a morning digest, keeping every
    other category in its configured order. The evening digests keep feed first."""
    last = [name for name in settings.morning_last_categories if name in cat_meta]
    first = {name: data for name, data in cat_meta.items() if name not in last}
    return {**first, **{name: cat_meta[name] for name in last}}


async def _fold_war_reports(cat_meta: dict, now: datetime) -> None:
    """Pull the war reports out of the feed into one block, in place: the LLM picks
    them and the one that sums up the period (the night, in a morning digest) leads it. Anything short of a
    usable answer leaves the category untouched — this is presentation, never a filter."""
    data = cat_meta.get(settings.war_block_category)
    if not data:
        return
    oldest = now.timestamp() - settings.war_block_max_age_hours * 3600
    candidates = sorted(
        (
            (source_name, item)
            for source_name, source_items in data["sources"].items()
            for item in source_items
            if (item["summary"] or "").strip() and not is_media_placeholder(item["summary"])
            and (_parse_published(item["published_at"]) or datetime.min.replace(tzinfo=timezone.utc)).timestamp() >= oldest
        ),
        # Oldest first, as the prompt tells the model.
        key=lambda pair: pair[1]["published_at"] or "",
    )
    if len(candidates) < settings.war_block_min_items:
        return
    # The time lets the model keep a previous evening's attack out of the night's figures.
    picked, overview = await pick_war_reports([
        {"id": i, "text": f"{_local_hhmm(item['published_at'])} {item['summary']}".strip()}
        for i, (_, item) in enumerate(candidates)
    ])
    if len(picked) < settings.war_block_min_items:
        return
    taken = {id(candidates[i][1]) for i in picked}
    for source_name in list(data["sources"]):
        data["sources"][source_name] = [it for it in data["sources"][source_name] if id(it) not in taken]
    # The country-wide tally leads, so a collapsed block still says what the night was;
    # each line keeps its channel's name, which the per-source blocks carried as a header.
    order = ([overview] if overview is not None else []) + [i for i in sorted(picked) if i != overview]
    data["war_block"] = {
        "items": [{**dict(candidates[i][1]), "_via": candidates[i][0]} for i in order],
        "title": _WAR_BLOCK_TITLE if _is_morning(now) else _WAR_BLOCK_TITLE_DAY,
    }
    log.info("War block: folded %d of %d %s item(s)", len(picked), len(candidates), settings.war_block_category)


def _digest_header(now: datetime, tags: str) -> str:
    """Opening line of a digest. Several digests go out per day, hence the time —
    the date alone labelled them all the same."""
    subline = now.strftime("%d %B")
    if tags:
        subline = f"{subline} · {tags}"
    # Italic carries the service bits (issue number, part marker, closing line) and
    # bold the title, so the chrome never uses a third typeface: a <code> chip
    # rendered as a grey monospace box and stood out more than the digest itself.
    return (
        f"<i>#{_digest_number(now)}</i>  <b>Digest</b>  <b>{now.strftime('%H:%M')}</b>\n"
        f"<i>{subline}</i>"
    )


def _part_marker(now: datetime, index: int, total: int) -> str:
    return f"<i>#{_digest_number(now)} · {index + 1}/{total}</i>"


def _digest_footer(now: datetime, item_count: int) -> str:
    label = "item" if item_count == 1 else "items"
    return f"<i>end #{_digest_number(now)} · {item_count} {label}</i>"


def _chrome_reserve(now: datetime, tags: str, item_count: int) -> int:
    """Room the header/part marker/footer will take once _decorate_messages runs.
    Reserved before splitting, or decorating could push a message past Telegram's
    limit and the send would fail."""
    # Part index/total are unknown before the split, so reserve for a marker far
    # wider than any real digest produces.
    head = max(len(_digest_header(now, tags)), len(_part_marker(now, 999, 999)))
    return head + len(_digest_footer(now, item_count)) + 4


def _decorate_messages(
    messages: list[tuple[str, list[int]]],
    now: datetime,
    tags: str,
    item_count: int,
) -> list[tuple[str, list[int]]]:
    """Wrap the split digest in its chrome: a header on the first message, a part
    marker on every continuation, a closing line on the last. Without it, the
    second message of one digest is indistinguishable from the first of the next."""
    total = len(messages)
    decorated: list[tuple[str, list[int]]] = []
    for index, (text, ids) in enumerate(messages):
        head = _digest_header(now, tags) if index == 0 else _part_marker(now, index, total)
        # Normalise the seam: a body may or may not start with the blank line that
        # separates categories, so strip it and always leave exactly one.
        stripped = text.lstrip("\n")
        body = f"{head}\n\n{stripped}"
        if index == total - 1:
            body = f"{body}\n\n{_digest_footer(now, item_count)}"
        decorated.append((body, ids))
    return decorated


def _split_into_messages(
    segments: list[tuple[str, list[int]]],
    reserve: int = 0,
) -> list[tuple[str, list[int]]]:
    limit = _TELEGRAM_LIMIT - reserve
    messages: list[tuple[str, list[int]]] = []
    cur_text, cur_ids = "", []
    for text, ids in segments:
        candidate = (cur_text + "\n" + text).lstrip("\n")
        if len(candidate) > limit:
            if cur_text:
                messages.append((cur_text, cur_ids))
            cur_text, cur_ids = text, list(ids)
        else:
            cur_text = candidate
            cur_ids = cur_ids + list(ids)
    if cur_text:
        messages.append((cur_text, cur_ids))
    return messages


async def _discard_building(building_msg_id: int | None) -> None:
    """Best-effort removal of the transient "Building digest..." status message."""
    if building_msg_id:
        try:
            await delete_message(building_msg_id)
        except Exception:
            pass


_RECLASSIFY_TIMEOUT = 120.0


async def _reclassify_empty_summaries(items: list, update: Callable[[str], Awaitable[None]]) -> list:
    """Re-run classification on items with an empty summary but non-empty raw
    text, bounded by a wall-clock timeout and per-model quota. Returns the
    (possibly rebuilt) list; items still empty fall through to _defer_empty_items."""
    empty = [item for item in items if needs_summary(item)]
    if not empty:
        return items
    log.info("Re-classifying %d item(s) with empty summary before digest (timeout=%ds)", len(empty), int(_RECLASSIFY_TIMEOUT))
    items = list(items)
    reclassify_start = time.monotonic()
    done = 0
    for i, item in enumerate(items):
        if needs_summary(item):
            if is_task_dead("classify"):
                remaining = sum(1 for x in items[i:] if not (x["summary"] or "").strip())
                log.warning("Re-classify aborted: all classify-task models quota dead, %d items will show as link", remaining)
                break
            elapsed = time.monotonic() - reclassify_start
            if elapsed > _RECLASSIFY_TIMEOUT:
                remaining = sum(1 for x in items[i:] if not (x["summary"] or "").strip())
                log.warning("Re-classify timeout after %.0fs, %d items will show as link", elapsed, remaining)
                break
            await update(f"⏳ Re-classifying {done + 1}/{len(empty)}...")
            raw = (item["raw_text"] or "").strip()
            if len(raw) < 15:
                await update_item_classification(item["id"], raw, "")
                items[i] = {**dict(item), "summary": raw, "key_phrase": ""}
                log.info("Short raw_text used as summary for item id=%d", item["id"])
            else:
                remaining_time = _RECLASSIFY_TIMEOUT - (time.monotonic() - reclassify_start)
                try:
                    result = await asyncio.wait_for(classify(raw, max_retries=3), timeout=max(5.0, remaining_time))
                except asyncio.TimeoutError:
                    log.info("Re-classify timed out on item id=%d, deferring it", item["id"])
                    result = ClassificationResult(summary="")
                if result.summary:
                    await update_item_classification(item["id"], result.summary, result.key_phrase)
                    items[i] = {**dict(item), "summary": result.summary, "key_phrase": result.key_phrase}
                    log.info("Re-classified item id=%d | summary=%s", item["id"], result.summary)
                else:
                    # Not an alert: _defer_empty_items holds the item back and the
                    # 20-minute background classify picks it up (measured: id=33860
                    # gave up at 19:30, was classified at 19:40). Only an item too old
                    # to defer is a real degradation, and that warns below.
                    log.info("Re-classify gave up on item id=%d, deferring it", item["id"])
            done += 1
    return items


async def _defer_empty_items(items: list) -> tuple[list, int]:
    """Resolve items still empty after re-classify: ones younger than
    _DEFER_MAX_DAYS are deferred for a later retry, older ones get a raw-text
    fallback summary. Returns (kept_items, deferred_count)."""
    now = datetime.now(timezone.utc)
    kept, deferred = [], 0
    for item in items:
        if not needs_summary(item):
            kept.append(item)
            continue
        ts = item["processed_at"] or item["published_at"]
        age_days = None
        if ts:
            try:
                age_days = (now - datetime.fromisoformat(ts)).total_seconds() / 86400
            except ValueError:
                age_days = None
        if age_days is not None and age_days < _DEFER_MAX_DAYS:
            deferred += 1
            continue
        raw = (item["raw_text"] or "").strip()
        fallback = "⚠️ " + raw[:80].split("\n")[0]
        await update_item_classification(item["id"], fallback, "")
        # This one IS worth waking the admin: the item has failed classification for
        # more than _DEFER_MAX_DAYS and now goes out as truncated raw text.
        log.warning("Item id=%d never classified in %d days, shipping raw text",
                    item["id"], _DEFER_MAX_DAYS)
        kept.append({**dict(item), "summary": fallback})
    return kept, deferred


async def _apply_semantic_filter(items: list) -> tuple[list, list]:
    """Run the LLM content filter over items that some rule targets. Blocked
    items are marked sent and returned separately for the Filtered section.
    Fail-open: on error the digest goes out unfiltered. Returns (kept, blocked)."""
    filter_rules_rows = await get_blocked_words()
    blocked_items: list = []
    if not filter_rules_rows:
        return items, blocked_items
    try:
        rules = [r["rule"] for r in filter_rules_rows]
        scope_map = await get_word_category_map()
        # Aligned with `rules`: None means the rule applies to every category.
        rule_scopes = [scope_map.get(r["id"]) or None for r in filter_rules_rows]

        def _has_applicable_rule(cat: str) -> bool:
            return any(scope is None or cat in scope for scope in rule_scopes)

        filterable, no_filter = [], []
        for item in items:
            prompt_extra = row_get(item, "source_prompt_extra")
            cat = item["category"] or "other"
            # Skip the LLM filter for items no rule targets (saves tokens), opted-out sources,
            # and media-only placeholders (unreadable content → any block would be a blind guess).
            if _wants_no_filter(prompt_extra) or not _has_applicable_rule(cat) or is_media_placeholder(item["summary"]):
                no_filter.append(item)
            else:
                filterable.append(item)
        check_input = [
            {
                "id": item["id"],
                "text": (item["summary"] or "") + " " + (item["raw_text"] or ""),
                "source": item["source_name"] or "unknown",
                "category": item["category"] or "other",
            }
            for item in filterable
        ]
        blocked_map = await check_blocked_filters(check_input, rules, rule_scopes)
        if blocked_map:
            for item in filterable:
                matched_rule = blocked_map.get(item["id"])
                if matched_rule is not None:
                    blocked_items.append({**item, "blocked_by": matched_rule})
                    log.info("Blocked item id=%d | rule=%r | summary=%s", item["id"], matched_rule, (item["summary"] or "")[:80])
            await mark_blocked([(item["id"], item["blocked_by"]) for item in blocked_items])
            log.info("Blocked %d item(s) by semantic filter", len(blocked_items))
        items = no_filter + [item for item in filterable if item["id"] not in blocked_map]
    except Exception:
        log.exception("Semantic filter failed, sending digest unfiltered")
        blocked_items = []
    return items, blocked_items


async def _run_within_source_merge(
    cat_meta: dict,
    source_prompt_extra: dict,
    vectors: dict,
    update: Callable[[str], Awaitable[None]],
) -> None:
    """Merge same-event items within each source in place, with progress updates."""
    # Embedding-based merge clusters cheaply, so it is worth running from 2 items;
    # the old LLM path only paid off in bulk (>= MERGE_MIN_ITEMS).
    merge_min = 2 if settings.merge_via_embeddings else MERGE_MIN_ITEMS
    sources_to_merge = [
        (cat_name, source_name)
        for cat_name, data in cat_meta.items()
        for source_name, source_items in data["sources"].items()
        if len(source_items) >= merge_min and not _wants_no_merge(source_prompt_extra.get(source_name))
    ]
    merge_stats = {"clusters": 0, "llm": 0, "near_dup": 0}
    merge_total = len(sources_to_merge)
    merge_done = 0
    if merge_total:
        await update(f"⏳ {_progress_bar(0, merge_total)} 0/{merge_total}")
    for cat_name, source_name in sources_to_merge:
        cat_meta[cat_name]["sources"][source_name] = await merge_source_items(
            cat_meta[cat_name]["sources"][source_name],
            prompt_extra=source_prompt_extra.get(source_name),
            vectors=vectors,
            stats=merge_stats,
        )
        merge_done += 1
        await update(f"⏳ {_progress_bar(merge_done, merge_total)} {merge_done}/{merge_total} — {source_name}")
    if merge_total:
        log.info(
            "Within-source merge: sources=%d clusters_merged=%d llm_calls=%d near-dup-skip=%d | mode=%s",
            merge_total, merge_stats["clusters"], merge_stats["llm"], merge_stats["near_dup"],
            "embeddings" if settings.merge_via_embeddings else "group_by_topic",
        )


async def _deliver(messages: list[tuple[str, list[int]]]) -> tuple[int, int, int, bool]:
    """Send each digest message, marking only items whose message actually
    reached Telegram (a mid-batch failure leaves the rest sent=0 for the next
    digest — no duplicates, no silent loss), then pin the first message.
    Returns (sent_count, total_messages, confirmed_count, failed)."""
    total_messages = len(messages)
    confirmed_ids: list[int] = []
    sent_count = 0
    first_message_id: int | None = None
    for msg_text, msg_ids in messages:
        try:
            msg_id = await send_message(msg_text, disable_notification=first_message_id is not None)
            if first_message_id is None:
                first_message_id = msg_id
            confirmed_ids.extend(msg_ids)
            sent_count += 1
        except Exception as exc:
            lost = total_messages - sent_count
            log.error(
                "Digest send failed at message %d/%d (%d message(s) and their items left unsent for retry): %s",
                sent_count + 1, total_messages, lost, exc,
            )
            break

    failed = sent_count < total_messages
    if confirmed_ids:
        await mark_sent(confirmed_ids)

    if first_message_id and not failed:
        prev_id = await get_app_setting("pinned_digest_message_id")
        if prev_id:
            await unpin_message(int(prev_id))
        await pin_message(first_message_id)
        await set_app_setting("pinned_digest_message_id", str(first_message_id))

    return sent_count, total_messages, len(confirmed_ids), failed


async def send_digest(
    categories: list[str] | None = None,
    include_quiet: bool = False,
    status_fn: Callable[[str], Awaitable[None]] | None = None,
) -> bool | None:
    if _digest_lock.locked():
        log.warning("Digest already in progress, skipping duplicate run | filter=%s", categories)
        return None
    async with _digest_lock:
        return await _send_digest_locked(categories, include_quiet, status_fn)


async def _send_digest_locked(
    categories: list[str] | None = None,
    include_quiet: bool = False,
    status_fn: Callable[[str], Awaitable[None]] | None = None,
) -> bool:
    digest_start = time.monotonic()

    async def _update(text: str) -> None:
        if status_fn:
            try:
                await status_fn(text)
            except Exception:
                pass
        if building_msg_id:
            try:
                await edit_message(building_msg_id, text)
            except Exception:
                pass

    items = await get_unsent_items(categories=categories)
    # The build works on every fetched item — re-classify, dedup, filter — long
    # before deferring or muting trims the list, so this, not what survives to the
    # message, is what the slow-digest threshold has to be measured against.
    processed_total = len(items)
    if not items:
        log.info("Digest triggered: no unsent items | filter=%s", categories)
        return False

    building_msg_id: int | None = None
    if not status_fn:
        try:
            # Silent: this status message is transient (edited, then deleted), so a
            # notification for it would ping the group twice per digest.
            building_msg_id = await send_message("⏳ Building digest...", disable_notification=True)
        except Exception:
            pass

    items = await _reclassify_empty_summaries(items, _update)

    items, deferred = await _defer_empty_items(items)
    if deferred:
        log.info("Deferred %d empty item(s) past digest (younger than %dd), will retry later", deferred, _DEFER_MAX_DAYS)
    if not items:
        log.info("Digest triggered: nothing to send after deferring %d empty item(s) | filter=%s", deferred, categories)
        await _discard_building(building_msg_id)
        return False

    items, blocked_items = await _apply_semantic_filter(items)
    if not items:
        log.info("Digest triggered: all items filtered by semantic filter | filter=%s", categories)
        await _discard_building(building_msg_id)
        return False

    # Embeddings are computed once here and shared by cross-source dedup and
    # within-source merge (both cluster on the same vectors).
    vectors: dict = {}
    if settings.dedup_enabled or settings.merge_via_embeddings:
        vectors = await ensure_embeddings(items)

    dup_link_map: dict[int, list[tuple[str, str]]] = {}
    if settings.dedup_enabled:
        items, dup_link_map = await deduplicate(items, vectors)
        if not items:
            log.info("Digest triggered: all items deduplicated away | filter=%s", categories)
            await _discard_building(building_msg_id)
            return False

    all_categories = await get_categories()
    cat_meta = {
        row["name"]: {"emoji": row["emoji"], "sources": defaultdict(list)}
        for row in all_categories
    }

    for item in items:
        cat = item["category"] or "other"
        if cat not in cat_meta:
            cat_meta[cat] = {"emoji": "📌", "sources": defaultdict(list)}
        source_name = item["source_name"] or "Unknown"
        cat_meta[cat]["sources"][source_name].append(item)

    source_prompt_extra: dict[str, str | None] = {}
    for item in items:
        sname = item["source_name"] or "Unknown"
        if sname not in source_prompt_extra:
            source_prompt_extra[sname] = row_get(item, "source_prompt_extra")

    await _run_within_source_merge(cat_meta, source_prompt_extra, vectors, _update)

    for data in cat_meta.values():
        for source_name, source_items in data["sources"].items():
            if len(source_items) > _MAX_ITEMS_PER_SOURCE:
                log.info(
                    "Source '%s': capped at %d (had %d merged items)",
                    source_name, _MAX_ITEMS_PER_SOURCE, len(source_items),
                )
                data["sources"][source_name] = source_items[:_MAX_ITEMS_PER_SOURCE]

    now = datetime.now(_get_tz())
    try:
        await _fold_war_reports(cat_meta, now)
    except Exception:
        log.exception("War block failed, rendering the feed unfolded")
    if _is_morning(now):
        cat_meta = _order_for_morning(cat_meta)

    segments = _build_digest_text(
        cat_meta,
        blocked_items=blocked_items,
        dup_links=dup_link_map,
    )
    if include_quiet:
        silent_block = await _build_silent_block()
        if silent_block:
            segments.append((silent_block, []))
            log.info("Appended quiet-sources block to digest")

    tags = _category_tags(cat_meta)
    messages = _split_into_messages(segments, reserve=_chrome_reserve(now, tags, len(items)))
    messages = _decorate_messages(messages, now, tags, len(items))

    await _update("⏳ Sending...")
    await _discard_building(building_msg_id)
    building_msg_id = None

    sent_count, total_messages, confirmed_count, failed = await _deliver(messages)

    status = "ok" if not failed else "partial"
    logged_total = len(items) + len(blocked_items)
    await log_digest(total=logged_total, status=status)
    elapsed = time.monotonic() - digest_start
    log.info(
        "Digest done: %d items (%d filtered) | %d/%d message(s) sent | %d items confirmed | filter=%s | status=%s | build=%.1fs",
        len(items), len(blocked_items), sent_count, total_messages, confirmed_count, categories, status, elapsed,
    )
    slow_warning = _slow_digest_warning(elapsed, processed_total)
    if slow_warning:
        log.warning("%s | filter=%s", slow_warning, categories)
    log.info(format_llm_stats())
    reset_llm_stats()
    return not failed

"""RSS raw-text composition: the headline (title) must not be dropped — some feeds
(e.g. FT) put the real news in the title and only a vague standfirst in the
description. _compose_raw_text combines distinct title+body, else uses whichever
is present without duplicating one inside the other."""
import pytest

from src.collectors import rss_collector
from src.collectors.rss_collector import _compose_raw_text


def test_distinct_title_and_body_are_combined():
    assert _compose_raw_text(
        "Apple raises MacBook prices by 20%",
        "iPhone maker blamed cost rises on memory chip shortages",
    ) == "Apple raises MacBook prices by 20%. iPhone maker blamed cost rises on memory chip shortages"


def test_body_containing_title_uses_body_only():
    # body already includes the headline → no duplication
    assert _compose_raw_text("Oil drops", "Oil drops below $70 a barrel as supply rises") == \
        "Oil drops below $70 a barrel as supply rises"


def test_title_containing_body_uses_title_only():
    assert _compose_raw_text("Full story: bank collapses overnight", "bank collapses") == \
        "Full story: bank collapses overnight"


def test_empty_body_falls_back_to_title():
    assert _compose_raw_text("Headline only", "") == "Headline only"


def test_empty_title_falls_back_to_body():
    assert _compose_raw_text("", "Body only standfirst") == "Body only standfirst"


def test_both_empty_returns_empty():
    assert _compose_raw_text("", "") == ""
    assert _compose_raw_text(None, None) == ""


def test_case_insensitive_containment():
    # title present in body case-insensitively → body only, no duplicate
    assert _compose_raw_text("BREXIT", "What brexit means for trade") == "What brexit means for trade"


class _Resp:
    """Minimal stand-in for a feedparser result."""

    def __init__(self, status, entries=()):
        self.status = status
        self.entries = list(entries)
        self.bozo = status != 200


def _fake_parse(by_agent):
    """feedparser.parse replacement returning a canned response per user agent."""
    calls = []

    def parse(url, agent=None):
        calls.append(agent)
        return by_agent[agent]

    return parse, calls


@pytest.mark.asyncio
async def test_ua_fallback_not_used_when_first_agent_works(monkeypatch):
    parse, calls = _fake_parse({rss_collector._FEED_AGENT: _Resp(200, ["a"])})
    monkeypatch.setattr(rss_collector.feedparser, "parse", parse)
    feed = await rss_collector._parse_with_ua_fallback("http://x/feed", "X")
    assert feed.status == 200
    assert calls == [rss_collector._FEED_AGENT]


@pytest.mark.asyncio
async def test_ua_fallback_retries_crawler_agent_on_403(monkeypatch):
    # jack-clark.net: Cloudflare 403s the browser UA from a server IP, serves the crawler UA.
    parse, calls = _fake_parse({
        rss_collector._FEED_AGENT: _Resp(403),
        rss_collector._FEED_AGENT_FALLBACK: _Resp(200, ["a", "b"]),
    })
    monkeypatch.setattr(rss_collector.feedparser, "parse", parse)
    feed = await rss_collector._parse_with_ua_fallback("http://x/feed", "X")
    assert feed.status == 200 and len(feed.entries) == 2
    assert calls == [rss_collector._FEED_AGENT, rss_collector._FEED_AGENT_FALLBACK]


@pytest.mark.asyncio
async def test_ua_fallback_keeps_original_status_when_both_blocked(monkeypatch):
    parse, _ = _fake_parse({
        rss_collector._FEED_AGENT: _Resp(403),
        rss_collector._FEED_AGENT_FALLBACK: _Resp(404),
    })
    monkeypatch.setattr(rss_collector.feedparser, "parse", parse)
    feed = await rss_collector._parse_with_ua_fallback("http://x/feed", "X")
    assert feed.status == 403  # the failure is reported as the real (first) block


@pytest.mark.asyncio
async def test_ua_fallback_does_not_retry_a_real_outage(monkeypatch):
    # 502/503 is the feed being down, not a UA block — retrying another UA is pointless.
    parse, calls = _fake_parse({rss_collector._FEED_AGENT: _Resp(502)})
    monkeypatch.setattr(rss_collector.feedparser, "parse", parse)
    feed = await rss_collector._parse_with_ua_fallback("http://x/feed", "X")
    assert feed.status == 502
    assert calls == [rss_collector._FEED_AGENT]

"""RSS raw-text composition: the headline (title) must not be dropped — some feeds
(e.g. FT) put the real news in the title and only a vague standfirst in the
description. _compose_raw_text combines distinct title+body, else uses whichever
is present without duplicating one inside the other."""
import logging

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


@pytest.mark.asyncio
@pytest.mark.parametrize("fails,remote,alerted,disabled", [
    (1, True, False, False),   # first miss of anything: quiet
    (2, True, False, False),   # hnrss 502 twice: the feed's outage, nothing to do
    (3, True, False, True),    # disabled and re-probed every 30 min — still nothing to do
    (7, True, False, True),
    (8, True, True, True),     # ~3h down: now the admin hears, once
    (9, True, False, True),    # ...and not again on every re-probe
    (1, False, False, False),
    (2, False, True, False),   # a repeated 4xx will not heal — a human must act
    (3, False, False, True),
    (8, False, True, True),    # fails 1-2 were 5xx, then the feed moved: still reported once
])
async def test_a_failing_feed_alerts_once_and_only_when_waiting_stopped_helping(
        monkeypatch, caplog, fails, remote, alerted, disabled):
    """2026-10-06: hnrss.org 502'd for under an hour and the admin still got two
    messages (a forwarded WARNING plus the Source error one) for an outage that healed."""
    sent, statuses = [], []

    async def fake_increment(source_id):
        return fails

    async def fake_status(source_id, status):
        statuses.append(status)

    async def fake_send(chat_id, text):
        sent.append(text)

    monkeypatch.setattr(rss_collector, "increment_source_fail_count", fake_increment)
    monkeypatch.setattr(rss_collector, "update_source_status", fake_status)
    monkeypatch.setattr(rss_collector, "send_to", fake_send)

    caplog.set_level(logging.DEBUG, logger=rss_collector.log.name)
    await rss_collector._mark_failure(1, "Hacker News", "u", "HTTP 502", remote_fault=remote)

    assert len(sent) == (1 if alerted else 0)
    assert statuses == (["error"] if disabled else [])
    # One channel: the message. A WARNING would be forwarded as a second copy.
    assert all(r.levelno < logging.WARNING for r in caplog.records)


def test_down_alert_escapes_what_the_feed_or_the_exception_said():
    text = rss_collector._down_alert("A&B <news>", "https://x/?a=1&b=2",
                                     "parse error: <urlopen error [Errno -2]>", 8, True)
    assert "<urlopen" not in text and "&lt;urlopen error" in text
    assert "A&amp;B &lt;news&gt;" in text and "a=1&amp;b=2" in text


@pytest.mark.parametrize("status,ours", [(404, True), (403, True), (410, True), (429, False), (408, False),
                                         (502, False), (503, False)])
def test_only_a_4xx_that_will_not_heal_counts_as_our_fault(status, ours):
    assert rss_collector._is_client_error(status) is ours

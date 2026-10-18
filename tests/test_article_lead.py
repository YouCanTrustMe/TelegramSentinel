"""A headline-only feed entry (Hacker News) gets a lead from its article page."""
import pytest
from feedparser.util import FeedParserDict

from src.collectors import rss_collector
from src.collectors.article_lead import body_from_html, lead_from_html
from src.config import settings as _settings


def test_the_page_description_is_the_lead():
    page = ('<html><head><meta property="og:description" content="Pele is a lightweight open source '
            'library that renders Mermaid diagrams to SVG, themed with CSS variables."></head></html>')
    assert lead_from_html(page).startswith("Pele is a lightweight open source library")


def test_a_short_description_falls_back_to_the_first_paragraphs():
    # alexwang.ai: description "Every layer is a memory store" says nothing about the post.
    para = "I have always had a foot in the self-hosting world, long before it was called that, and here is why."
    page = (f'<meta name="description" content="Every layer is a memory store">'
            f'<nav><p>{"menu " * 30}</p></nav><p>short</p><p>{para}</p><p>{para}</p><p>{para}</p>')
    lead = lead_from_html(page)
    assert "menu" not in lead and "Every layer" not in lead
    assert lead == f"{para} {para}"


def test_a_site_wide_boilerplate_description_is_skipped():
    readme = "DEsk is a skeuomorphic Wayland desktop environment for Linux, written in Rust with a retro look."
    page = ('<meta property="og:description" content="Contribute to fyzugx/DEsk development by creating an '
            f'account on GitHub."><p>{readme}</p>')
    assert lead_from_html(page) == readme


def test_an_apostrophe_does_not_cut_the_description():
    page = ('<meta property="og:description" content="Apple\'s new M5 chip doubles the neural engine and '
            'ships first in the 14-inch MacBook Pro.">')
    assert lead_from_html(page).startswith("Apple's new M5 chip doubles the neural engine")
    page = ("<meta content='The \"fast path\" of the kernel scheduler was rewritten for big.LITTLE chips.' "
            "property='og:description'>")
    assert lead_from_html(page).startswith('The "fast path" of the kernel scheduler')


def test_svg_paths_and_code_blocks_are_not_paragraphs():
    para = "The team rebuilt the parser from scratch after a year of patching the old one, and here is the story."
    page = f'<svg><path d="M0 0">{"icon menu " * 12}</svg></p><pre>{"x = 1 " * 30}</pre><p class="lead">{para}</p>'
    assert lead_from_html(page) == para


def test_a_page_without_text_gives_no_lead():
    assert lead_from_html("<html><body><div>app shell</div><script>var x</script></body></html>") == ""


def test_only_listed_feed_hosts_fetch_from_the_page():
    lead, body = _settings.rss_article_lead_hosts, _settings.rss_article_body_hosts
    assert rss_collector._host_listed("https://hnrss.org/newest?points=500", lead)
    assert not rss_collector._host_listed("https://www.croatiaweek.com/feed/", lead)
    assert rss_collector._host_listed("https://www.croatiaweek.com/feed/", body)
    assert not rss_collector._host_listed("https://www.index.hr/rss", body)


def test_a_teaser_feed_reads_the_opening_paragraphs_not_the_description():
    # Croatia Week: the page description repeats the feed's teaser; the paragraphs carry
    # what the teaser cut ("the minister announced extra points for doctors…").
    paras = [f"Paragraph {i} of the article, long enough to count as real body text for the reader here."
             for i in range(5)]
    page = ('<meta property="og:description" content="Croatia is planning new measures to improve '
            'healthcare on islands and in rural areas.">' + "".join(f"<p>{p}</p>" for p in paras))
    assert body_from_html(page) == " ".join(paras[:3])


class _Resp:
    status = 200
    bozo = False

    def __init__(self, entries):
        self.entries = entries


@pytest.mark.asyncio
async def test_a_hacker_news_entry_is_stored_with_its_article_lead(tmp_path, monkeypatch):
    from src.config import settings
    from src.db.base import init_db
    from src.db.models import add_source, get_unsent_items

    monkeypatch.setattr(settings, "database_path", str(tmp_path / "t.db"))
    await init_db()
    sid = await add_source("rss", "Hacker News", "https://hnrss.org/newest?points=500", "tech")
    entries = [
        FeedParserDict(link="https://pele.run/", title="Pele – Mermaid diagrams as themeable SVG",
                       summary="Article URL: https://pele.run/ Comments URL: https://news.ycombinator.com/item?id=1 "
                               "Points: 508 # Comments: 46"),
        FeedParserDict(link="https://blocked.example/", title="A paywalled story",
                       summary="Article URL: https://blocked.example/ Points: 600"),
    ]
    leads = {"https://pele.run/": "Pele renders Mermaid diagrams to SVG, themed with CSS variables."}

    async def _parse(url, name):
        return _Resp(entries)

    async def _lead(url):
        return leads.get(url, "")

    monkeypatch.setattr(rss_collector, "_parse_with_ua_fallback", _parse)
    monkeypatch.setattr(rss_collector, "fetch_article_lead", _lead)
    assert await rss_collector.fetch_feed(sid, "Hacker News", "https://hnrss.org/newest?points=500", "tech") == 2

    texts = {row["original_url"]: row["raw_text"] for row in await get_unsent_items()}
    assert texts["https://pele.run/"] == ("Pele – Mermaid diagrams as themeable SVG. "
                                          "Pele renders Mermaid diagrams to SVG, themed with CSS variables.")
    # No lead: the headline alone, without hnrss's link and points boilerplate.
    assert texts["https://blocked.example/"] == "A paywalled story"


@pytest.mark.asyncio
async def test_a_croatia_week_teaser_is_replaced_by_the_article_opening(tmp_path, monkeypatch):
    from src.db.base import init_db
    from src.db.models import add_source, get_unsent_items

    monkeypatch.setattr(_settings, "database_path", str(tmp_path / "t.db"))
    await init_db()
    feed_url = "https://www.croatiaweek.com/feed/"
    sid = await add_source("rss", "Croatia Week", feed_url, "hrvatska")
    entries = [
        FeedParserDict(link="https://www.croatiaweek.com/a/", title="Island healthcare",
                       summary="Croatia is planning new measures to improve access to healthcare on its […]"),
        FeedParserDict(link="https://www.croatiaweek.com/b/", title="Cravat Day",
                       summary="Croatia marked World Cravat Day on 18 October […]"),
    ]
    bodies = {"https://www.croatiaweek.com/a/": "Croatia is planning new measures to improve access to "
                                                "healthcare on its islands. The minister announced extra points."}

    async def _parse(url, name):
        return _Resp(entries)

    async def _body(url):
        return bodies.get(url, "")

    monkeypatch.setattr(rss_collector, "_parse_with_ua_fallback", _parse)
    monkeypatch.setattr(rss_collector, "fetch_article_body", _body)
    assert await rss_collector.fetch_feed(sid, "Croatia Week", feed_url, "hrvatska") == 2

    texts = {row["original_url"]: row["raw_text"] for row in await get_unsent_items()}
    assert texts["https://www.croatiaweek.com/a/"].endswith("The minister announced extra points.")
    # The page could not be read: the feed's teaser stays.
    assert texts["https://www.croatiaweek.com/b/"] == "Croatia marked World Cravat Day on 18 October […]"

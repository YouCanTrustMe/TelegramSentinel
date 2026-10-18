"""More text for a feed entry than its feed gives, read from the linked page: a short
lead for a headline-only entry, or the opening paragraphs for a teaser."""
import html
import logging
import re

import aiohttp

log = logging.getLogger(__name__)

_TIMEOUT = aiohttp.ClientTimeout(total=10)
_MAX_BYTES = 400_000
_MAX_CHARS = 600
_MAX_BODY_CHARS = 900
_BODY_PARAGRAPHS = 3
_MIN_DESCRIPTION = 60
_MIN_PARAGRAPH = 80
_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

# The value runs to the quote that opened it ("Apple's new chip" keeps its apostrophe),
# never past the tag.
_META_RES = [
    re.compile(rf'<meta[^>]+{attr}=["\']{name}["\'][^>]+content=(["\'])(?P<text>(?:(?!\1)[^<>])+)\1', re.I)
    for attr, name in (("property", "og:description"), ("name", "description"), ("name", "twitter:description"))
] + [re.compile(r'<meta[^>]+content=(["\'])(?P<text>(?:(?!\1)[^<>])+)\1[^>]+property=["\']og:description', re.I)]
# Site-wide boilerplate a description field carries on every page of a host: it says
# nothing about the article, so the page's paragraphs are read instead.
_BOILERPLATE_RE = re.compile(
    r"development by creating an account on GitHub|We.re on a journey to advance and democratize", re.I
)
_NOISE_RE = re.compile(r"(?is)<(script|style|nav|header|footer|aside|form)[^>]*>.*?</\1>")
_PARAGRAPH_RE = re.compile(r"(?is)<p(?:\s[^>]*)?>(.*?)</p>")  # not <path>, <pre>, <picture>
_TAG_RE = re.compile(r"<[^>]+>")


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub(" ", text))).strip()


def _paragraphs(page: str) -> list[str]:
    body = _NOISE_RE.sub(" ", page)
    return [p for p in (_clean(m) for m in _PARAGRAPH_RE.findall(body)) if len(p) >= _MIN_PARAGRAPH]


def lead_from_html(page: str) -> str:
    """The page's description when it says something (60+ chars), else its first two
    paragraphs of 80+ chars, capped at a few sentences. Empty when the page has neither."""
    for pattern in _META_RES:
        match = pattern.search(page)
        text = _clean(match.group("text")) if match else ""
        if len(text) >= _MIN_DESCRIPTION and not _BOILERPLATE_RE.search(text):
            return text[:_MAX_CHARS]
    return " ".join(_paragraphs(page)[:2])[:_MAX_CHARS]


def body_from_html(page: str) -> str:
    """The article's opening paragraphs, for a page whose description only repeats the
    feed's teaser."""
    return " ".join(_paragraphs(page)[:_BODY_PARAGRAPHS])[:_MAX_BODY_CHARS]


async def fetch_article_lead(url: str) -> str:
    return await _fetch(url, lead_from_html)


async def fetch_article_body(url: str) -> str:
    return await _fetch(url, body_from_html)


async def _fetch(url: str, extract) -> str:
    """Never raises: a blocked, slow or non-HTML page leaves the entry as its headline.
    Only a 200 counts — a 402/403 page is a paywall or a bot check, not the article."""
    if not url.startswith(("http://", "https://")):
        return ""
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT, headers={"User-Agent": _AGENT}) as session:
            async with session.get(url) as resp:
                if resp.status != 200 or "html" not in resp.headers.get("Content-Type", ""):
                    log.info("Article lead skipped: HTTP %s %s | %s", resp.status,
                             resp.headers.get("Content-Type", "?"), url[:120])
                    return ""
                # read(n) returns as soon as ONE chunk arrives: a chunked page came back as
                # its first 694 bytes and no paragraph at all.
                raw = bytearray()
                async for chunk in resp.content.iter_chunked(65536):
                    raw.extend(chunk)
                    if len(raw) >= _MAX_BYTES:
                        break
                page = bytes(raw).decode(resp.charset or "utf-8", "ignore")
    except Exception as exc:
        log.info("Article lead skipped: %s | %s", type(exc).__name__, url[:120])
        return ""
    text = extract(page)
    log.info("Article %s: %d chars | %s", extract.__name__.split("_")[0], len(text), url[:120])
    return text

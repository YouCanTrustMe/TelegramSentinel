"""Reads the picture of a Telegram post that says little in words.

A week of digests (2026-11-05..12) held 96 photo/video posts whose own text had under 80 letters; the model saw
only «[Photo] Лох» or «[Photo] По факту» while the picture carried the news (a death notice, a screenshot of a
mayor's complaint). The collector downloads such a picture and appends what this module reads from it, so the
summary, the content filter, the fact check and dedup all see it."""
import asyncio
import base64
import logging
import re

from src.common.media import MEDIA_TOKENS
from src.processor.llm.classifier import _TRIVIAL_MAX_LEN, _as_text
from src.processor.llm.llm_client import llm_json
from src.processor.llm.prompts import IMAGE_READ_PROMPT

log = logging.getLogger(__name__)

# Fewer letters than this in the post's own words and the picture is read: the line under which the summary
# pass shows a short Ukrainian post as written, counted in letters so emoji and the collector's tags do not count.
SHORT_TEXT_LETTERS = _TRIVIAL_MAX_LEN
# The whole chain walk for one picture, failovers included: the collector waits for it inside a poll.
_READ_TIMEOUT = 45.0
KINDS = ("news", "meme", "reaction", "ad", "announcement", "other")
_DESCRIPTION_MAX = 200
_IMAGE_TEXT_MAX = 400
_CAPTION_MAX = 300

_TAG_RE = re.compile(r"\[(?:Context|Forwarded from)[^\]\n]*\]|" + "|".join(re.escape(t) for t in sorted(MEDIA_TOKENS)))
_LETTER_RE = re.compile(r"[^\W\d_]")


def own_words(text: str) -> str:
    """The post's own words: the collector's media token, forward and reply labels taken off."""
    return _TAG_RE.sub("", (text or "").strip()).strip()


def wants_image_read(text: str) -> bool:
    return len(_LETTER_RE.findall(own_words(text))) < SHORT_TEXT_LETTERS


def _one_line(value, limit: int) -> str:
    """A model field as one tidy line, cut at a word boundary. Anything not a string is no answer: a dict in a
    text slot once reached sqlite (2026-09-08). Square brackets would end the collector's tag early."""
    text = " ".join(_as_text(value).split()).replace("[", "(").replace("]", ")")
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + "…"
    return text


def image_note(answer) -> str | None:
    """The line the collector puts on the post, or None when the answer says nothing. The model's `kind` is left
    out: it called a death-notice screenshot a «meme» (11-12), and a guess in the text would steer the filter."""
    if not isinstance(answer, dict):
        return None
    description = _one_line(answer.get("image"), _DESCRIPTION_MAX)
    on_image = _one_line(answer.get("image_text"), _IMAGE_TEXT_MAX).strip("«»\"' ")
    if not description and not on_image:
        return None
    parts = [description.rstrip(".") + "." if description else "", f"Text on it: «{on_image}»" if on_image else ""]
    return f"[Image: {' '.join(p for p in parts if p)}]"


async def read_image(data: bytes, caption: str) -> str | None:
    """What the picture shows, as the line to append; None on any failure, so the post is kept as it was."""
    prompt = IMAGE_READ_PROMPT.replace("{caption}", _one_line(own_words(caption), _CAPTION_MAX))
    messages = [{"role": "user", "content": [
        {"type": "text", "text": prompt},
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(data).decode()}},
    ]}]
    try:
        answer = await asyncio.wait_for(llm_json(messages, task="image"), _READ_TIMEOUT)
    except asyncio.TimeoutError:
        log.info("Image read: no answer within %.0fs", _READ_TIMEOUT)
        return None
    except Exception as exc:
        log.info("Image read failed: %r", exc)
        return None
    note = image_note(answer)
    if note:
        log.info("Image read: kind=%s", answer.get("kind") if answer.get("kind") in KINDS else "other")
    return note

"""Single source of truth for caption-less media posts: the token the collector
tags them with and the emoji chip the digest renders. Driving both sides from this
one table means a new media type is added in exactly one place."""
import re


# Ordered: caption detection picks the first matching Pyrogram Message attribute.
MEDIA_TYPES: list[tuple[str, str, str]] = [
    # (pyrogram Message attribute, token, digest emoji)
    ("photo", "[Photo]", "📷"),
    ("video", "[Video]", "🎬"),
    ("animation", "[GIF]", "🎞️"),
    ("video_note", "[Video note]", "🔵"),
    ("sticker", "[Sticker]", "🔖"),
    ("document", "[Doc]", "📎"),
    ("audio", "[Audio]", "🎵"),
    ("voice", "[Voice]", "🎤"),
]

# Media with no attribute of its own above, told apart by Message.media (a
# MessageMediaType name). Before these, a giveaway and its results reached the digest
# as a bare "📦 Media" (a news channel, 2026-10-12/17).
MEDIA_KINDS: list[tuple[str, str, str, str]] = [
    # (MessageMediaType name, token, digest emoji, digest word)
    ("GIVEAWAY", "[Giveaway]", "🎁", "Giveaway"),
    ("GIVEAWAY_WINNERS", "[Giveaway results]", "🎁", "Giveaway results"),
    ("STORY", "[Story]", "📖", "Story"),
    ("LOCATION", "[Location]", "📍", "Location"),
    ("VENUE", "[Venue]", "📍", "Place"),
    ("CONTACT", "[Contact]", "👤", "Contact"),
    ("CHECKLIST", "[Checklist]", "☑️", "Checklist"),
    ("PAID_MEDIA", "[Paid media]", "⭐", "Paid media"),
    ("DICE", "[Dice]", "🎲", "Dice"),
    ("GAME", "[Game]", "🎮", "Game"),
    ("INVOICE", "[Invoice]", "🧾", "Invoice"),
]
MEDIA_KIND_TOKEN: dict[str, str] = {kind: token for kind, token, _, _ in MEDIA_KINDS}

# The picture of a post with few words, read by a vision model (processor/llm/image_reader.py). The collector puts
# it FIRST, so a channel signature stays the last line; only the forward label goes before it.
IMAGE_LINE_RE = re.compile(r"^(\[Forwarded from [^\]\n]*\] )?\[Image: [^\n]*\]\n")


def has_image_note(text: str | None) -> bool:
    return bool(IMAGE_LINE_RE.search(_after_context(text or "")))


def without_image_note(text: str | None) -> str:
    """The post as its channel wrote it: richness, the unsigned-ad check and the like must not count what a
    vision model read off the picture. A reply's "[Context: …]" line stays where it is."""
    text = text or ""
    context = text[: len(text) - len(_after_context(text))]
    return context + IMAGE_LINE_RE.sub(lambda m: m.group(1) or "", _after_context(text), count=1)


def _after_context(text: str) -> str:
    return re.sub(r"^\[Context: [^\n]*\]\n", "", text, count=1)


# Media we cannot name (an undecodable post, or a type missing from both tables).
GENERIC_MEDIA_TOKEN = "[Media]"
# Marker value stored for a generic-media post; never shown literally (renders 📦).
NO_TEXT = "no text"

MEDIA_EMOJI: dict[str, str] = {token: emoji for _, token, emoji in MEDIA_TYPES}
MEDIA_EMOJI.update({token: emoji for _, token, emoji, _ in MEDIA_KINDS})
MEDIA_EMOJI[NO_TEXT] = "📦"
MEDIA_EMOJI[GENERIC_MEDIA_TOKEN] = "📦"

# A bare emoji is too small a tap target for the digest link, so a media-only post
# renders as "<emoji> <word>" (e.g. "📷 Photo") and the link anchors on the word.
_MEDIA_WORD: dict[str, str] = {
    "[Photo]": "Photo",
    "[Video]": "Video",
    "[GIF]": "GIF",
    "[Video note]": "Video",
    "[Sticker]": "Sticker",
    "[Doc]": "File",
    "[Audio]": "Audio",
    "[Voice]": "Voice",
    GENERIC_MEDIA_TOKEN: "Media",
    NO_TEXT: "Media",
    **{token: word for _, token, _, word in MEDIA_KINDS},
}

MEDIA_LABEL: dict[str, str] = {
    token: f"{emoji} {_MEDIA_WORD.get(token, '')}".strip() for token, emoji in MEDIA_EMOJI.items()
}

# Bare tokens that, with no caption, mean a media-only post.
MEDIA_TOKENS: set[str] = (
    {token for _, token, _ in MEDIA_TYPES} | set(MEDIA_KIND_TOKEN.values()) | {GENERIC_MEDIA_TOKEN}
)


def is_media_placeholder(summary: str | None) -> bool:
    """True when a post carries no readable text — a media token, the no-text marker,
    or empty. The content filter must never judge (or block) these: we can't see what
    they contain, so a block would be a blind guess."""
    t = (summary or "").strip()
    return t == "" or t == NO_TEXT or t in MEDIA_TOKENS

import asyncio
import logging
import time

from src.config import settings
from src.dispatcher.sender import send_to

log = logging.getLogger(__name__)

_THROTTLE_SECONDS = 3600
_WHITELIST_PREFIXES = (
    "src.collectors.",
    "src.processor.",
    "src.dispatcher.",
    "src.scheduler",
)

_last_sent: dict[str, float] = {}
_lock = asyncio.Lock()
_loop: asyncio.AbstractEventLoop | None = None


def set_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _loop
    _loop = loop


async def admin_alert(text: str, key: str | None = None, silent: bool = True) -> bool:
    """Send a silent message to the admin, throttled by `key` (defaults to text)."""
    throttle_key = key or text
    now = time.monotonic()
    async with _lock:
        last = _last_sent.get(throttle_key)
        if last is not None and now - last < _THROTTLE_SECONDS:
            return False
        _last_sent[throttle_key] = now
    try:
        await send_to(settings.telegram_admin_id, text, disable_notification=silent)
        return True
    except Exception as exc:
        log.warning("admin_alert failed: %s", exc)
        return False


class AdminAlertLogHandler(logging.Handler):
    """Forwards WARNING/ERROR from whitelisted loggers to admin chat, throttled per message."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno < logging.WARNING:
            return
        if record.name == __name__ or record.name.endswith(".admin_alert"):
            return
        if not any(record.name.startswith(p) for p in _WHITELIST_PREFIXES):
            return
        try:
            msg = self.format(record)
        except Exception:
            return
        if _loop is None or not _loop.is_running():
            return
        text = f"⚠️ <code>{record.levelname}</code> <b>{record.name}</b>\n<pre>{_html_escape(msg)[:1500]}</pre>"
        key = f"{record.name}:{record.levelno}:{record.msg}"
        try:
            asyncio.run_coroutine_threadsafe(admin_alert(text, key=key), _loop)
        except Exception:
            pass


def _html_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

"""Screen-level helpers in src/bot/handlers/misc.py: the log tail has to survive
Telegram's message limit, which a long collector error used to break."""
from src.bot.handlers.misc import _LOG_HEADER_BUDGET, _TELEGRAM_TEXT_LIMIT, _fit_log_lines


def _rendered_len(lines):
    from html import escape
    return len(escape("\n".join(lines)))


def test_short_tail_is_untouched():
    lines = ["line one", "line two", "line three"]
    assert _fit_log_lines(lines) == lines


def test_oversized_tail_is_trimmed_from_the_top():
    lines = [f"{i:03d} " + "x" * 400 for i in range(20)]
    fitted = _fit_log_lines(lines)
    assert _rendered_len(fitted) <= _TELEGRAM_TEXT_LIMIT - _LOG_HEADER_BUDGET
    # The newest line is the one being read, so it must survive the trim.
    assert fitted[-1] == lines[-1]
    assert lines[0] not in fitted


def test_escaping_counts_towards_the_budget():
    # 1000 '<' escape to 4000 chars: under the limit raw, over it once escaped.
    lines = ["<" * 1000, "tail"]
    fitted = _fit_log_lines(lines)
    assert fitted == ["tail"]


def test_single_oversized_line_is_cut_rather_than_leaving_an_empty_tail():
    # A traceback on one line used to empty the whole reply: "last 0 lines".
    fitted = _fit_log_lines(["y" * 9000])
    assert len(fitted) == 1
    assert fitted[0].endswith("…")
    assert _rendered_len(fitted) <= _TELEGRAM_TEXT_LIMIT - _LOG_HEADER_BUDGET


def test_an_oversized_newest_line_survives_after_older_ones_are_dropped():
    fitted = _fit_log_lines(["old", "<" * 5000])
    assert len(fitted) == 1 and fitted[0].startswith("&lt;") is False
    assert _rendered_len(fitted) <= _TELEGRAM_TEXT_LIMIT - _LOG_HEADER_BUDGET


def test_clipping_a_huge_line_is_not_quadratic():
    # One traceback with no newlines used to be trimmed a character at a time,
    # re-escaping the whole string each pass, on the loop that runs the collectors.
    import time
    start = time.monotonic()
    fitted = _fit_log_lines(["<" * 2_000_000])
    assert _rendered_len(fitted) <= _TELEGRAM_TEXT_LIMIT - _LOG_HEADER_BUDGET
    assert time.monotonic() - start < 2

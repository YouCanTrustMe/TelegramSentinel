"""Schedule-string algebra shared by the scheduler and the timetable UI.

A category's schedule is stored as a comma-separated `digest_time` string, so every
reader has to agree on how it parses. Keeping that agreement here — with no imports
from either side — is what stops the timetable from showing one thing while cron
runs another, and keeps the bot from depending on the job runner just to read a time.
"""
import logging

log = logging.getLogger(__name__)


def parse_times(time_str: str) -> list[tuple[int, int]]:
    result = []
    for t in (time_str or "").split(","):
        t = t.strip()
        if not t:
            continue
        try:
            h, m = map(int, t.split(":"))
            # Range-checked here so an out-of-range time can never reach CronTrigger:
            # it would raise on every rebuild AND on startup, taking the whole
            # schedule down until the stored value was edited by hand.
            if not (0 <= h < 24 and 0 <= m < 60):
                raise ValueError(t)
            result.append((h, m))
        except (ValueError, AttributeError):
            log.warning("Invalid time in schedule string: %r", t)
    return result


def format_times(times) -> str:
    """Canonical digest_time string: zero-padded, sorted, de-duplicated. Stored
    values drifted into a mix of "11:00,21:00" and "11:00, 16:00, 21:00"; every
    write now goes through here."""
    return ",".join(sorted({f"{h:02d}:{m:02d}" for h, m in times}))


def with_time(digest_time: str, time_str: str) -> str:
    return format_times(parse_times(digest_time) + parse_times(time_str))


def without_time(digest_time: str, time_str: str) -> str:
    dropped = set(parse_times(time_str))
    return format_times([t for t in parse_times(digest_time) if t not in dropped])


def fires_at(cat, time_str: str) -> bool:
    return set(parse_times(time_str)) <= set(parse_times(cat["digest_time"]))


def slots_by_time(cats) -> dict[str, list]:
    """Map "HH:MM" -> categories firing at that time, ordered by time."""
    slots: dict[str, list] = {}
    for cat in cats:
        for h, m in parse_times(cat["digest_time"]):
            slots.setdefault(f"{h:02d}:{m:02d}", []).append(cat)
    return {t: slots[t] for t in sorted(slots)}

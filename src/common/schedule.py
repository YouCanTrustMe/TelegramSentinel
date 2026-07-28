"""Reading a category's schedule, shared by the scheduler and the timetable UI.

Times are stored one per row in `category_times`; `get_categories` hands them back
as a canonical `digest_time` string for display, and these helpers are how that
string is read. Writes never build it — they go through the (hour, minute) helpers
in the DB layer. Keeping the reading here, with no imports from either side, is
what keeps the bot from depending on the job runner just to show a time.
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


def fires_at(cat, time_str: str) -> bool:
    return set(parse_times(time_str)) <= set(parse_times(cat["digest_time"]))


def slots_by_time(cats) -> dict[str, list]:
    """Map "HH:MM" -> categories firing at that time, ordered by time."""
    slots: dict[str, list] = {}
    for cat in cats:
        for h, m in parse_times(cat["digest_time"]):
            slots.setdefault(f"{h:02d}:{m:02d}", []).append(cat)
    return {t: slots[t] for t in sorted(slots)}

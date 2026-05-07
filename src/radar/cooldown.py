import time

_cooldowns: dict[tuple[str, int], float] = {}


def is_on_cooldown(keyword: str, chat_id: int, seconds: int) -> bool:
    key = (keyword.lower(), chat_id)
    last = _cooldowns.get(key)
    return last is not None and (time.monotonic() - last) < seconds


def set_cooldown(keyword: str, chat_id: int) -> None:
    _cooldowns[(keyword.lower(), chat_id)] = time.monotonic()

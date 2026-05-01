import hashlib

from src.db.models import is_seen


def make_message_id(source_type: str, source_url: str, raw_id: str) -> str:
    if source_type == "telegram":
        return f"tg_{source_url}_{raw_id}"
    return hashlib.md5(f"{source_url}:{raw_id}".encode()).hexdigest()


async def is_duplicate(message_id: str) -> bool:
    return await is_seen(message_id)

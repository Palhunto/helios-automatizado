from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4


def new_id() -> str:
    return str(uuid4())


def is_uuid4(value: str) -> bool:
    try:
        parsed = UUID(value)
    except (ValueError, AttributeError):
        return False
    return parsed.version == 4 and str(parsed) == value.lower()


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")

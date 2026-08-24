from __future__ import annotations

from urllib.parse import urlparse
from uuid import UUID


def is_real_conversation_path(path: str) -> bool:
    """Return whether *path* is a canonical ChatGPT conversation URL path."""
    parts = path.split("/")
    if len(parts) != 3 or parts[:2] != ["", "c"] or not parts[2]:
        return False
    try:
        conversation_id = UUID(parts[2])
    except ValueError:
        return False
    return str(conversation_id) == parts[2].lower()


def conversation_path_from_page_url(url: str) -> str | None:
    """Extract a real conversation path only from an observed ChatGPT page URL."""
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname != "chatgpt.com":
        return None
    return parsed.path if is_real_conversation_path(parsed.path) else None

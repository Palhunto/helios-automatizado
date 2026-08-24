from __future__ import annotations

import unicodedata

from ebook_pipeline.core.errors import IntegrityError
from ebook_pipeline.core.hashing import sha256_bytes


def transport_normalize(text: str) -> str:
    """Normalize only DOM transport differences; never use this as an artifact hash."""
    normalized = unicodedata.normalize("NFKC", text).replace("\u00a0", " ")
    normalized = normalized.replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(line.rstrip() for line in normalized.split("\n")).strip()


def transport_fingerprint(text: str) -> str:
    return sha256_bytes(transport_normalize(text).encode("utf-8"))


def strict_request_text(content: bytes) -> str:
    try:
        return content.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise IntegrityError(
            "BROWSER_REQUEST_ENCODING_INVALID", "Request artifact must be strict UTF-8"
        ) from exc


def captured_response_bytes(text: str) -> bytes:
    return text.encode("utf-8", errors="strict")

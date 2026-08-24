from __future__ import annotations

import re
import unicodedata
from collections import Counter

WHITESPACE_PATTERN = re.compile(r"\s+")
MARKDOWN_LINK_PATTERN = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
MARKDOWN_PREFIX_PATTERN = re.compile(r"(?m)^\s*(?:#{1,6}\s+|>\s?|[-+*]\s+|\d+[.)]\s+)")
MARKDOWN_DECORATION_PATTERN = re.compile(r"[`*_~]")
WORD_PATTERN = re.compile(r"\w+", re.UNICODE)


def compare_capture_text(rendered: str, copied: str) -> dict[str, object]:
    first_index = _first_divergence_index(rendered, copied)
    whitespace_equal = _normalize_whitespace(rendered) == _normalize_whitespace(copied)
    formatting_equal = _normalize_formatting(rendered) == _normalize_formatting(copied)
    if rendered == copied:
        classification = "exact"
    elif whitespace_equal:
        classification = "whitespace"
    elif formatting_equal:
        classification = "markdown_formatting"
    else:
        classification = "content"

    rendered_tokens = Counter(_tokens(_normalize_formatting(rendered)))
    copied_tokens = Counter(_tokens(_normalize_formatting(copied)))
    rendered_only_count = sum((rendered_tokens - copied_tokens).values())
    copied_only_count = sum((copied_tokens - rendered_tokens).values())
    substantive = classification == "content" and bool(
        rendered_only_count or copied_only_count
    )
    return {
        "classification": classification,
        "copied": {
            "byte_length": len(copied.encode("utf-8")),
            "character_length": len(copied),
        },
        "first_divergence": _divergence_payload(rendered, copied, first_index),
        "rendered": {
            "byte_length": len(rendered.encode("utf-8")),
            "character_length": len(rendered),
        },
        "substantive_difference": {
            "copied_has_text_absent_from_rendered": substantive and copied_only_count > 0,
            "copied_only_token_count": copied_only_count,
            "present": substantive,
            "rendered_has_text_absent_from_copied": substantive and rendered_only_count > 0,
            "rendered_only_token_count": rendered_only_count,
        },
    }


def _first_divergence_index(left: str, right: str) -> int | None:
    for index, (left_char, right_char) in enumerate(zip(left, right, strict=False)):
        if left_char != right_char:
            return index
    return None if len(left) == len(right) else min(len(left), len(right))


def _divergence_payload(left: str, right: str, index: int | None) -> dict[str, object] | None:
    if index is None:
        return None
    return {
        "copied_character": _character_payload(right, index),
        "copied_context": right[max(0, index - 20) : index + 21],
        "index": index,
        "rendered_character": _character_payload(left, index),
        "rendered_context": left[max(0, index - 20) : index + 21],
    }


def _character_payload(value: str, index: int) -> dict[str, str] | None:
    if index >= len(value):
        return None
    character = value[index]
    return {"codepoint": f"U+{ord(character):04X}", "value": character}


def _normalize_whitespace(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).replace("\r\n", "\n").replace("\r", "\n")
    return WHITESPACE_PATTERN.sub(" ", normalized).strip()


def _normalize_formatting(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    normalized = MARKDOWN_LINK_PATTERN.sub(r"\1", normalized)
    normalized = MARKDOWN_PREFIX_PATTERN.sub("", normalized)
    normalized = MARKDOWN_DECORATION_PATTERN.sub("", normalized)
    return _normalize_whitespace(normalized)


def _tokens(value: str) -> list[str]:
    return [token.casefold() for token in WORD_PATTERN.findall(value)]

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class AnchorCandidate(BaseModel):
    """Operational V1 payload: no IDs, offsets or inferred editorial fields."""

    model_config = ConfigDict(extra="forbid", strict=True)
    anchor_text: str = Field(min_length=1)
    position_relative_to_anchor: Literal["before", "after"]
    anchor_before: str | None = None
    anchor_after: str | None = None


@dataclass(frozen=True, slots=True)
class AnchorValidation:
    valid: bool
    code: str | None
    occurrence_count: int
    unit_id: str | None = None
    start_offset: int | None = None
    end_offset: int | None = None
    byte_start: int | None = None
    byte_end: int | None = None


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def parse_candidate(raw: bytes) -> AnchorCandidate | None:
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
        return AnchorCandidate.model_validate(value)
    except (UnicodeDecodeError, ValueError, ValidationError):
        return None


def validate_candidate(
    candidate: AnchorCandidate | None, page: dict[str, Any], text: str
) -> AnchorValidation:
    if candidate is None:
        return AnchorValidation(False, "VISUAL_ANCHOR_PAYLOAD_INVALID", 0)
    if not candidate.anchor_text.strip():
        return AnchorValidation(False, "VISUAL_ANCHOR_EMPTY", 0)
    spans = page["unit_spans"]
    page_start = min(int(span["global_char_start"]) for span in spans)
    page_end = max(int(span["global_char_end"]) for span in spans)
    positions: list[int] = []
    cursor = page_start
    while cursor < page_end:
        found = text.find(candidate.anchor_text, cursor, page_end)
        if found < 0:
            break
        positions.append(found)
        cursor = found + 1  # Includes overlapping occurrences.
    if len(positions) != 1:
        return AnchorValidation(False, "VISUAL_ANCHOR_NOT_UNIQUE_IN_PAGE", len(positions))
    start = positions[0]
    end = start + len(candidate.anchor_text)
    owners = [
        span
        for span in spans
        if int(span["global_char_start"]) <= start < end <= int(span["global_char_end"])
    ]
    if len(owners) != 1:
        return AnchorValidation(False, "VISUAL_ANCHOR_CROSSES_UNIT_BOUNDARY", 1)
    before, after = candidate.anchor_before, candidate.anchor_after
    if (
        before is not None
        and not text[page_start:start].endswith(before)
        or after is not None
        and not text[end:page_end].startswith(after)
    ):
        return AnchorValidation(False, "VISUAL_ANCHOR_CONTEXT_MISMATCH", 1)
    return AnchorValidation(
        True,
        None,
        1,
        str(owners[0]["unit_id"]),
        start,
        end,
        len(text[:start].encode("utf-8")),
        len(text[:end].encode("utf-8")),
    )


def candidate_report(raw: bytes, page: dict[str, Any], text: str) -> dict[str, Any]:
    candidate = parse_candidate(raw)
    return {
        "candidate": None if candidate is None else candidate.model_dump(),
        "validation": asdict(validate_candidate(candidate, page, text)),
    }

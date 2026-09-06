from __future__ import annotations

from typing import Any

import pytest

from ebook_pipeline.visual_planning.anchors import (
    AnchorCandidate,
    parse_candidate,
    validate_candidate,
)


def _page(boundary: int = 10, end: int = 20) -> dict[str, Any]:
    return {
        "unit_spans": [
            {"unit_id": "CH01_A", "global_char_start": 0, "global_char_end": boundary},
            {"unit_id": "CH01_B", "global_char_start": boundary, "global_char_end": end},
        ]
    }


@pytest.mark.parametrize(
    "raw",
    [
        b"{}",
        b"[]",
        b"null",
        b"\xff",
        b"```json\n{}\n```",
        b'{"anchor_text":"x","position_relative_to_anchor":"side"}',
        b'{"anchor_text":"x","position_relative_to_anchor":"after","unit_id":"CH01_A"}',
        b'{"anchor_text":"x","anchor_text":"y","position_relative_to_anchor":"after"}',
        b'{"anchor_text":12,"position_relative_to_anchor":"after"}',
    ],
)
def test_anchor_payload_is_strict_and_never_invents_fields(raw: bytes) -> None:
    assert parse_candidate(raw) is None


@pytest.mark.parametrize(
    ("text", "anchor", "expected"),
    [
        ("abcdefghijABCDEFGHIJ", "ijAB", "VISUAL_ANCHOR_CROSSES_UNIT_BOUNDARY"),
        ("abcdefghijabcdefghij", "abc", "VISUAL_ANCHOR_NOT_UNIQUE_IN_PAGE"),
        ("aaaaabcdefABCDEFGHIJ", "aaa", "VISUAL_ANCHOR_NOT_UNIQUE_IN_PAGE"),
        ("abcdefghijABCDEFGHIJ", "missing", "VISUAL_ANCHOR_NOT_UNIQUE_IN_PAGE"),
        ("abcdefghijABCDEFGHIJ", " ", "VISUAL_ANCHOR_EMPTY"),
    ],
)
def test_anchor_rejects_ambiguous_crossing_or_absent_literals(
    text: str, anchor: str, expected: str
) -> None:
    candidate = AnchorCandidate(anchor_text=anchor, position_relative_to_anchor="after")
    result = validate_candidate(candidate, _page(), text)
    assert result.valid is False
    assert result.code == expected
    assert result.unit_id is None


def test_anchor_resolves_late_unicode_offsets_and_ignores_other_pages() -> None:
    text = "ação 🧠 A.\nconceito B.\nconceito B."
    candidate = AnchorCandidate(anchor_text="conceito", position_relative_to_anchor="before")
    result = validate_candidate(candidate, _page(10, 22), text)
    assert result.valid is True
    assert result.unit_id == "CH01_B"
    assert text[result.start_offset : result.end_offset] == "conceito"
    assert text.encode()[result.byte_start : result.byte_end].decode() == "conceito"
    assert result.byte_start != result.start_offset


def test_optional_context_is_literal_and_does_not_override_ambiguity() -> None:
    candidate = AnchorCandidate(
        anchor_text="ABC", position_relative_to_anchor="after", anchor_before="wrong"
    )
    assert validate_candidate(candidate, _page(), "abcdefghijABCDEFGHIJ").code == (
        "VISUAL_ANCHOR_CONTEXT_MISMATCH"
    )

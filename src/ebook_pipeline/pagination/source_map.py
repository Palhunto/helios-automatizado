from __future__ import annotations

import re

from ebook_pipeline.core.errors import IntegrityError
from ebook_pipeline.core.hashing import sha256_bytes
from ebook_pipeline.pagination.contracts import PaginationLayout
from ebook_pipeline.pagination.models import (
    ConsolidationMemberSource,
    PageUnitSpan,
    PaginationFragment,
    SourceLedger,
    SourceUnit,
)

_BLANK_LINE = re.compile(r"(?:\r\n|\n|\r)[ \t]*(?:\r\n|\n|\r)+")


def utf8_byte_offset(text: str, char_offset: int) -> int:
    if char_offset < 0 or char_offset > len(text):
        raise ValueError("character offset is outside the text")
    return len(text[:char_offset].encode("utf-8"))


def build_source_ledger(
    *,
    consolidated_bytes: bytes,
    separator: str,
    members: tuple[ConsolidationMemberSource, ...],
    layout: PaginationLayout,
) -> SourceLedger:
    ordered_members = tuple(sorted(members, key=lambda item: item.unit_order))
    expected_units = layout.ordered_units()
    if [item.unit_id for item in ordered_members] != [item.unit_id for item in expected_units]:
        raise IntegrityError(
            "PAGINATION_UNIT_MAPPING_MISMATCH",
            "Consolidation members do not match the versioned pagination layout",
        )
    for expected_order, member in enumerate(ordered_members, start=1):
        if member.unit_order != expected_order:
            raise IntegrityError(
                "PAGINATION_UNIT_ORDER_INVALID",
                "Consolidation member order must be contiguous",
            )
        if sha256_bytes(member.content) != member.sha256:
            raise IntegrityError(
                "PAGINATION_UNIT_SOURCE_HASH_MISMATCH",
                f"Accepted source bytes for {member.unit_id!r} differ from provenance",
            )
    separator_bytes = separator.encode("utf-8")
    reconstructed = separator_bytes.join(item.content for item in ordered_members)
    if reconstructed != consolidated_bytes:
        raise IntegrityError(
            "PAGINATION_CONSOLIDATION_RECONSTRUCTION_MISMATCH",
            "Accepted member bytes and separator do not reproduce the consolidation",
        )
    try:
        consolidated_text = consolidated_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise IntegrityError(
            "PAGINATION_CONSOLIDATION_UTF8_INVALID",
            "Text consolidation is not valid UTF-8",
        ) from exc

    units: list[SourceUnit] = []
    global_char = 0
    global_byte = 0
    for member, mapping in zip(ordered_members, expected_units, strict=True):
        try:
            text = member.content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise IntegrityError(
                "PAGINATION_UNIT_UTF8_INVALID",
                f"Accepted source for {member.unit_id!r} is not valid UTF-8",
            ) from exc
        units.append(
            SourceUnit(
                unit_id=member.unit_id,
                unit_order=member.unit_order,
                chapter_id=mapping.chapter_id,
                eligible=mapping.eligible,
                starts_new_page=mapping.starts_new_page,
                artifact_id=member.artifact_id,
                sha256=member.sha256,
                text=text,
                global_char_start=global_char,
                global_char_end=global_char + len(text),
                global_byte_start=global_byte,
                global_byte_end=global_byte + len(member.content),
            )
        )
        global_char += len(text) + len(separator)
        global_byte += len(member.content) + len(separator_bytes)
    if units:
        global_char -= len(separator)
        global_byte -= len(separator_bytes)
    if global_char != len(consolidated_text) or global_byte != len(consolidated_bytes):
        raise IntegrityError(
            "PAGINATION_OFFSET_LEDGER_MISMATCH",
            "Reversible character/byte ledger does not cover the consolidation",
        )
    return SourceLedger(consolidated_text, consolidated_bytes, tuple(units))


def fragments_for_unit(
    unit: SourceUnit,
    layout: PaginationLayout,
) -> tuple[PaginationFragment, ...]:
    ranges: list[tuple[int, int]] = []
    cursor = 0
    for match in _BLANK_LINE.finditer(unit.text):
        ranges.append((cursor, match.end()))
        cursor = match.end()
    if cursor < len(unit.text):
        ranges.append((cursor, len(unit.text)))
    if not ranges and unit.text:
        ranges.append((0, len(unit.text)))

    headings = {item.exact_text: item.level for item in layout.heading_rules}
    fragments: list[PaginationFragment] = []
    for start, end in ranges:
        text = unit.text[start:end]
        core = text.rstrip("\r\n")
        heading_level = (
            headings.get(core) if "\n" not in core and "\r" not in core else None
        )
        fragments.append(
            _fragment(
                unit,
                "heading" if heading_level is not None else "paragraph",
                start,
                end,
                heading_level=heading_level,
            )
        )
    if "".join(item.text for item in fragments) != unit.text:
        raise IntegrityError(
            "PAGINATION_FRAGMENT_REVERSIBILITY_FAILED",
            f"Pagination fragments do not reconstruct unit {unit.unit_id!r}",
        )
    return tuple(fragments)


def split_fragment(
    fragment: PaginationFragment, split_char: int
) -> tuple[PaginationFragment, PaginationFragment]:
    if split_char <= 0 or split_char >= len(fragment.text):
        raise ValueError("fragment split must leave non-empty sides")
    if fragment.kind == "heading":
        raise ValueError("headings cannot be split")
    left_text = fragment.text[:split_char]
    left_byte_count = len(left_text.encode("utf-8"))
    left = PaginationFragment(
        unit_id=fragment.unit_id,
        kind=fragment.kind,
        heading_level=None,
        text=left_text,
        global_char_start=fragment.global_char_start,
        global_char_end=fragment.global_char_start + split_char,
        global_byte_start=fragment.global_byte_start,
        global_byte_end=fragment.global_byte_start + left_byte_count,
        unit_char_start=fragment.unit_char_start,
        unit_char_end=fragment.unit_char_start + split_char,
        unit_byte_start=fragment.unit_byte_start,
        unit_byte_end=fragment.unit_byte_start + left_byte_count,
    )
    right = PaginationFragment(
        unit_id=fragment.unit_id,
        kind=fragment.kind,
        heading_level=None,
        text=fragment.text[split_char:],
        global_char_start=left.global_char_end,
        global_char_end=fragment.global_char_end,
        global_byte_start=left.global_byte_end,
        global_byte_end=fragment.global_byte_end,
        unit_char_start=left.unit_char_end,
        unit_char_end=fragment.unit_char_end,
        unit_byte_start=left.unit_byte_end,
        unit_byte_end=fragment.unit_byte_end,
    )
    return left, right


def spans_for_page(
    fragments: tuple[PaginationFragment, ...],
    units: tuple[SourceUnit, ...],
) -> tuple[PageUnitSpan, ...]:
    unit_by_id = {item.unit_id: item for item in units}
    ordered_ids: list[str] = []
    for fragment in fragments:
        if fragment.unit_id not in ordered_ids:
            ordered_ids.append(fragment.unit_id)
    spans: list[PageUnitSpan] = []
    for span_order, unit_id in enumerate(ordered_ids, start=1):
        selected = [item for item in fragments if item.unit_id == unit_id]
        unit = unit_by_id[unit_id]
        spans.append(
            PageUnitSpan(
                unit_id=unit_id,
                span_order=span_order,
                artifact_id=unit.artifact_id,
                source_sha256=unit.sha256,
                global_char_start=selected[0].global_char_start,
                global_char_end=selected[-1].global_char_end,
                global_byte_start=selected[0].global_byte_start,
                global_byte_end=selected[-1].global_byte_end,
                unit_char_start=selected[0].unit_char_start,
                unit_char_end=selected[-1].unit_char_end,
                unit_byte_start=selected[0].unit_byte_start,
                unit_byte_end=selected[-1].unit_byte_end,
            )
        )
    return tuple(spans)


def _fragment(
    unit: SourceUnit,
    kind: str,
    unit_char_start: int,
    unit_char_end: int,
    *,
    heading_level: int | None,
) -> PaginationFragment:
    start_bytes = utf8_byte_offset(unit.text, unit_char_start)
    end_bytes = utf8_byte_offset(unit.text, unit_char_end)
    return PaginationFragment(
        unit_id=unit.unit_id,
        kind="heading" if kind == "heading" else "paragraph",
        heading_level=(
            heading_level if heading_level in {1, 2, 3} and kind == "heading" else None
        ),
        text=unit.text[unit_char_start:unit_char_end],
        global_char_start=unit.global_char_start + unit_char_start,
        global_char_end=unit.global_char_start + unit_char_end,
        global_byte_start=unit.global_byte_start + start_bytes,
        global_byte_end=unit.global_byte_start + end_bytes,
        unit_char_start=unit_char_start,
        unit_char_end=unit_char_end,
        unit_byte_start=start_bytes,
        unit_byte_end=end_bytes,
    )

from __future__ import annotations

import json
from typing import Any

from ebook_pipeline.core.errors import IntegrityError
from ebook_pipeline.pagination.models import VisualPaginationSnapshot


def validate_manifest_structure(
    snapshot: VisualPaginationSnapshot,
    manifest_bytes: bytes,
) -> dict[str, Any]:
    try:
        value = json.loads(manifest_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IntegrityError(
            "PAGINATION_MANIFEST_INVALID",
            f"Pagination manifest is not valid UTF-8 JSON: {exc}",
        ) from exc
    if not isinstance(value, dict) or value.get("schema") != "helios_pagination_snapshot@1":
        raise IntegrityError(
            "PAGINATION_MANIFEST_SCHEMA_INVALID",
            "Pagination manifest must use helios_pagination_snapshot@1",
        )
    expected_scalars = {
        "snapshot_id": snapshot.id,
        "project_id": snapshot.project_id,
        "version": snapshot.version,
        "input_hash": snapshot.input_hash,
        "document_page_count": snapshot.document_page_count,
        "eligible_page_count": snapshot.eligible_page_count,
    }
    for field, expected in expected_scalars.items():
        if value.get(field) != expected:
            raise IntegrityError(
                "PAGINATION_MANIFEST_BINDING_MISMATCH",
                f"Pagination manifest field {field!r} differs from SQLite",
            )
    consolidation = value.get("consolidation")
    if not isinstance(consolidation, dict) or any(
        consolidation.get(field) != expected
        for field, expected in {
            "id": snapshot.consolidation_id,
            "context_id": snapshot.context_id,
            "production_set_hash": snapshot.production_set_hash,
            "text_artifact_id": snapshot.text_artifact_id,
            "text_sha256": snapshot.text_sha256,
            "manifest_artifact_id": snapshot.consolidation_manifest_artifact_id,
            "manifest_sha256": snapshot.consolidation_manifest_sha256,
        }.items()
    ):
        raise IntegrityError(
            "PAGINATION_MANIFEST_CONSOLIDATION_MISMATCH",
            "Pagination manifest consolidation provenance differs from SQLite",
        )
    writing_contract = consolidation.get("writing_contract")
    if writing_contract != {
        "id": snapshot.writing_contract_id,
        "version": snapshot.writing_contract_version,
        "sha256": snapshot.writing_contract_sha256,
    }:
        raise IntegrityError(
            "PAGINATION_MANIFEST_WRITING_CONTRACT_MISMATCH",
            "Pagination manifest writing contract provenance differs from SQLite",
        )
    layout = value.get("layout")
    if not isinstance(layout, dict) or any(
        layout.get(field) != expected
        for field, expected in {
            "id": snapshot.layout_id,
            "version": snapshot.layout_version,
            "sha256": snapshot.layout_sha256,
        }.items()
    ):
        raise IntegrityError(
            "PAGINATION_MANIFEST_LAYOUT_MISMATCH",
            "Pagination manifest layout provenance differs from SQLite",
        )
    renderer = value.get("renderer")
    if not isinstance(renderer, dict) or renderer.get("fingerprint") != (
        snapshot.renderer_fingerprint
    ):
        raise IntegrityError(
            "PAGINATION_MANIFEST_RENDERER_MISMATCH",
            "Pagination manifest renderer fingerprint differs from SQLite",
        )
    artifacts = value.get("artifacts")
    if not isinstance(artifacts, dict):
        raise IntegrityError(
            "PAGINATION_MANIFEST_ARTIFACTS_INVALID",
            "Pagination manifest artifact provenance is missing",
        )
    html = artifacts.get("html")
    pdf = artifacts.get("pdf")
    if (
        not isinstance(html, dict)
        or not isinstance(pdf, dict)
        or html.get("sha256") != snapshot.html_sha256
        or pdf.get("sha256") != snapshot.pdf_sha256
    ):
        raise IntegrityError(
            "PAGINATION_MANIFEST_ARTIFACTS_MISMATCH",
            "Pagination manifest HTML/PDF hashes differ from SQLite",
        )
    pages = value.get("pages")
    if not isinstance(pages, list) or len(pages) != snapshot.document_page_count:
        raise IntegrityError(
            "PAGINATION_MANIFEST_PAGE_COUNT_INVALID",
            "Pagination manifest pages do not match document_page_count",
        )
    eligible = [page for page in pages if isinstance(page, dict) and page.get("eligible") is True]
    if len(eligible) != snapshot.eligible_page_count:
        raise IntegrityError(
            "PAGINATION_MANIFEST_ELIGIBLE_COUNT_INVALID",
            "Pagination manifest eligible pages do not match eligible_page_count",
        )
    if [page.get("document_page_number") for page in pages] != list(
        range(1, len(pages) + 1)
    ):
        raise IntegrityError(
            "PAGINATION_DOCUMENT_NUMBERING_INVALID",
            "Document page numbers must be globally contiguous",
        )
    if [page.get("eligible_page_number") for page in eligible] != list(
        range(1, len(eligible) + 1)
    ):
        raise IntegrityError(
            "PAGINATION_ELIGIBLE_NUMBERING_INVALID",
            "Eligible page numbers must be globally contiguous",
        )
    page_keys: set[str] = set()
    chapter_numbers: dict[str, int] = {}
    expected_chapters = {f"CH{number:02d}" for number in range(1, 9)}
    for page in pages:
        if not isinstance(page, dict):
            raise IntegrityError(
                "PAGINATION_MANIFEST_PAGE_INVALID", "Pagination page must be an object"
            )
        spans = page.get("unit_spans")
        if not isinstance(spans, list) or not spans:
            raise IntegrityError(
                "PAGINATION_PAGE_SPANS_MISSING",
                "Every pagination page must contain at least one typed unit span",
            )
        if page.get("eligible") is True:
            chapter_id = page.get("chapter_id")
            chapter_page_number = page.get("chapter_page_number")
            if not isinstance(chapter_id, str) or not isinstance(chapter_page_number, int):
                raise IntegrityError(
                    "PAGINATION_ELIGIBLE_PAGE_BINDING_INVALID",
                    "Eligible page requires typed chapter and internal page number",
                )
            expected_chapter_number = chapter_numbers.get(chapter_id, 0) + 1
            if chapter_page_number != expected_chapter_number:
                raise IntegrityError(
                    "PAGINATION_CHAPTER_NUMBERING_INVALID",
                    "Chapter page numbers must reset and remain contiguous",
                )
            chapter_numbers[chapter_id] = chapter_page_number
            page_key = page.get("page_key")
            if page_key != f"{chapter_id}-P{chapter_page_number:03d}":
                raise IntegrityError(
                    "PAGINATION_PAGE_KEY_INVALID", "Eligible page_key is not canonical"
                )
            if page_key in page_keys:
                raise IntegrityError(
                    "PAGINATION_PAGE_KEY_DUPLICATE", "Eligible page_key must be unique"
                )
            page_keys.add(page_key)
            span_units = {
                span.get("unit_id") for span in spans if isinstance(span, dict)
            }
            if not span_units or any(
                unit_id not in {f"{chapter_id}_A", f"{chapter_id}_B"}
                for unit_id in span_units
            ):
                raise IntegrityError(
                    "PAGINATION_PAGE_UNIT_CHAPTER_MISMATCH",
                    "Eligible page spans must belong to the same typed chapter",
                )
            if page.get("visual_slot") is None:
                raise IntegrityError(
                    "PAGINATION_VISUAL_SLOT_MISSING",
                    "Every eligible page must reserve exactly one visual slot",
                )
        elif any(
            page.get(field) is not None
            for field in (
                "eligible_page_number",
                "chapter_id",
                "chapter_page_number",
                "page_key",
                "visual_slot",
            )
        ):
            raise IntegrityError(
                "PAGINATION_NONELIGIBLE_BINDING_INVALID",
                "Noneligible page cannot expose chapter binding or visual slot",
            )
    if set(chapter_numbers) != expected_chapters:
        raise IntegrityError(
            "PAGINATION_ELIGIBLE_CHAPTER_COVERAGE_INVALID",
            "Eligible pages must cover exactly CH01 through CH08",
        )
    return value


def validate_anchor_candidate(
    manifest: dict[str, Any],
    consolidated_text: str,
    *,
    page_key: str,
    unit_id: str,
    anchor_text: str,
) -> tuple[int, int]:
    if not anchor_text:
        raise IntegrityError("VISUAL_ANCHOR_EMPTY", "Anchor text must not be empty")
    pages = manifest.get("pages")
    if not isinstance(pages, list):
        raise IntegrityError("PAGINATION_MANIFEST_INVALID", "Manifest pages are missing")
    matches = [
        page
        for page in pages
        if isinstance(page, dict) and page.get("page_key") == page_key
    ]
    if len(matches) != 1:
        raise IntegrityError(
            "VISUAL_ANCHOR_PAGE_INVALID", "Anchor page_key must resolve exactly one eligible page"
        )
    spans = matches[0].get("unit_spans")
    if not isinstance(spans, list):
        raise IntegrityError("VISUAL_ANCHOR_UNIT_INVALID", "Page unit spans are missing")
    unit_spans = [
        span for span in spans if isinstance(span, dict) and span.get("unit_id") == unit_id
    ]
    if len(unit_spans) != 1:
        raise IntegrityError(
            "VISUAL_ANCHOR_UNIT_INVALID",
            "Anchor unit must resolve exactly one typed span inside its assigned page",
        )
    span = unit_spans[0]
    start = span.get("global_char_start")
    end = span.get("global_char_end")
    if not isinstance(start, int) or not isinstance(end, int):
        raise IntegrityError("VISUAL_ANCHOR_SPAN_INVALID", "Anchor source span is invalid")
    page_unit_text = consolidated_text[start:end]
    positions: list[int] = []
    cursor = 0
    while True:
        found = page_unit_text.find(anchor_text, cursor)
        if found < 0:
            break
        positions.append(found)
        cursor = found + 1
    if len(positions) != 1:
        raise IntegrityError(
            "VISUAL_ANCHOR_NOT_UNIQUE_IN_PAGE_UNIT_SPAN",
            "Anchor must occur exactly once inside the assigned page_key and typed unit span",
            evidence={"occurrence_count": len(positions)},
        )
    absolute_start = start + positions[0]
    absolute_end = absolute_start + len(anchor_text)
    if absolute_end > end:
        raise IntegrityError(
            "VISUAL_ANCHOR_OUTSIDE_PAGE_UNIT_SPAN",
            "Anchor interval crosses its assigned page or unit span",
        )
    return absolute_start, absolute_end

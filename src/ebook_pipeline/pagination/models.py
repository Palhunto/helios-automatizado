from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class RendererFingerprint:
    fingerprint: str
    os_name: str
    os_release: str
    os_version: str
    machine: str
    python_version: str
    playwright_version: str
    pypdf_version: str
    chromium_executable_sha256: str


@dataclass(frozen=True, slots=True)
class SourceUnit:
    unit_id: str
    unit_order: int
    chapter_id: str | None
    eligible: bool
    starts_new_page: bool
    artifact_id: str
    sha256: str
    text: str
    global_char_start: int
    global_char_end: int
    global_byte_start: int
    global_byte_end: int


@dataclass(frozen=True, slots=True)
class ConsolidationMemberSource:
    unit_id: str
    unit_order: int
    artifact_id: str
    sha256: str
    content: bytes


@dataclass(frozen=True, slots=True)
class SourceLedger:
    text: str
    text_bytes: bytes
    units: tuple[SourceUnit, ...]


@dataclass(frozen=True, slots=True)
class PaginationFragment:
    unit_id: str
    kind: Literal["paragraph", "heading"]
    heading_level: int | None
    text: str
    global_char_start: int
    global_char_end: int
    global_byte_start: int
    global_byte_end: int
    unit_char_start: int
    unit_char_end: int
    unit_byte_start: int
    unit_byte_end: int


@dataclass(frozen=True, slots=True)
class PageUnitSpan:
    unit_id: str
    span_order: int
    artifact_id: str
    source_sha256: str
    global_char_start: int
    global_char_end: int
    global_byte_start: int
    global_byte_end: int
    unit_char_start: int
    unit_char_end: int
    unit_byte_start: int
    unit_byte_end: int


@dataclass(frozen=True, slots=True)
class PaginationPage:
    document_page_number: int
    eligible: bool
    eligible_page_number: int | None
    chapter_id: str | None
    chapter_page_number: int | None
    page_key: str | None
    page_source_sha256: str
    fragments: tuple[PaginationFragment, ...]
    unit_spans: tuple[PageUnitSpan, ...]


@dataclass(frozen=True, slots=True)
class RenderedPagination:
    html: bytes
    pdf: bytes
    pages: tuple[PaginationPage, ...]
    renderer: RendererFingerprint


@dataclass(frozen=True, slots=True)
class VisualPaginationSnapshot:
    id: str
    project_id: str
    stage_run_id: str
    version: int
    input_hash: str
    consolidation_id: str
    context_id: str
    production_set_hash: str
    text_artifact_id: str
    text_sha256: str
    consolidation_manifest_artifact_id: str
    consolidation_manifest_sha256: str
    writing_contract_id: str
    writing_contract_version: int
    writing_contract_sha256: str
    layout_id: str
    layout_version: int
    layout_sha256: str
    renderer_fingerprint: str
    html_sha256: str | None
    pdf_sha256: str | None
    manifest_sha256: str | None
    html_artifact_id: str | None
    pdf_artifact_id: str | None
    manifest_artifact_id: str | None
    document_page_count: int
    eligible_page_count: int
    created_at: str

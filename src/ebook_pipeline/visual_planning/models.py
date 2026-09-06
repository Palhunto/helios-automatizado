from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

VisualPlanDisposition = Literal["processing", "valid", "invalid"]


@dataclass(frozen=True, slots=True)
class ParsedVisualFigure:
    number: int
    name: str
    editorial_page: str
    section: str
    exact_position: str
    main_concept: str
    conceptual_synthesis: str
    justification: str
    objective: str
    visual_type: str
    complexity: str
    generation_prompt: str


@dataclass(frozen=True, slots=True)
class ValidationFinding:
    code: str
    message: str
    figure_order: int | None = None
    page_key: str | None = None


@dataclass(frozen=True, slots=True)
class BoundVisualFigure:
    editorial: ParsedVisualFigure
    figure_order: int
    document_page_number: int
    eligible_page_number: int
    page_key: str
    chapter_id: str
    page_unit_ids: tuple[str, ...]
    page_unit_span_orders: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class VisualPlan:
    id: str
    project_id: str
    stage_run_id: str
    raw_version: int
    disposition: VisualPlanDisposition
    input_hash: str
    pagination_snapshot_id: str
    pagination_manifest_artifact_id: str
    pagination_manifest_sha256: str
    consolidation_id: str
    context_id: str
    production_set_hash: str
    text_artifact_id: str
    text_sha256: str
    consolidation_manifest_artifact_id: str
    consolidation_manifest_sha256: str
    prompt_id: str
    prompt_version: int
    prompt_sha256: str
    raw_sha256: str
    raw_artifact_id: str | None
    validation_report_sha256: str | None
    validation_report_artifact_id: str | None
    expected_figure_count: int
    figure_count: int
    created_at: str
    validated_at: str | None


@dataclass(frozen=True, slots=True)
class VisualFigure:
    id: str
    project_id: str
    visual_plan_id: str
    pagination_snapshot_id: str
    figure_order: int
    number: int
    name: str
    editorial_page: str
    section: str
    exact_position: str
    main_concept: str
    conceptual_synthesis: str
    justification: str
    objective: str
    visual_type: str
    complexity: str
    generation_prompt: str
    document_page_number: int
    eligible_page_number: int
    page_key: str
    chapter_id: str
    editorial_sha256: str
    page_unit_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class VisualAnchor:
    id: str
    project_id: str
    visual_plan_id: str
    figure_id: str
    stage_run_id: str
    version: int
    input_hash: str
    supersedes_anchor_id: str | None
    disposition: VisualPlanDisposition
    prompt_id: str
    prompt_version: int
    prompt_sha256: str
    raw_sha256: str
    raw_artifact_id: str | None
    report_sha256: str | None
    report_artifact_id: str | None
    unit_id: str | None
    start_offset: int | None
    end_offset: int | None
    created_at: str
    validated_at: str | None


@dataclass(frozen=True, slots=True)
class VisualFinalization:
    id: str
    project_id: str
    visual_plan_id: str
    stage_run_id: str
    accepted_version: int
    input_hash: str
    manifest_artifact_id: str | None
    manifest_sha256: str | None
    created_at: str
    accepted_at: str | None


type VisualPlanSources = tuple[VisualPlan, list[VisualFigure], dict[str, Any], str]

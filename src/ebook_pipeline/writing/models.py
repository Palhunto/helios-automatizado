from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ebook_pipeline.core.models import Artifact, RunStatus


class SubmissionDisposition(StrEnum):
    PROCESSING = "processing"
    ACCEPTED = "accepted"
    REVIEW_REQUIRED = "review_required"
    REJECTED = "rejected"


class FindingSeverity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


class ProductionUnitStatus(StrEnum):
    CURRENT_COMPATIBLE = "current_compatible"
    MISSING_ACCEPTED = "missing_accepted"
    HISTORICAL_INCOMPATIBLE = "historical_incompatible"


@dataclass(frozen=True, slots=True)
class WritingContext:
    id: str
    project_id: str
    stage_run_id: str
    version: int
    input_hash: str
    answers_document_id: str
    answers_artifact_id: str
    answers_sha256: str
    plan_document_id: str
    plan_artifact_id: str
    plan_sha256: str
    writing_prompt_id: str
    writing_prompt_version: int
    writing_prompt_sha256: str
    contract_id: str
    contract_version: int
    contract_sha256: str
    request_prompt_id: str
    request_prompt_version: int
    request_prompt_sha256: str
    package_sha256: str
    manifest_sha256: str
    package_artifact_id: str | None
    manifest_artifact_id: str | None
    created_at: str


@dataclass(frozen=True, slots=True)
class WritingAcknowledgement:
    id: str
    project_id: str
    context_id: str
    stage_run_id: str
    raw_version: int
    raw_sha256: str
    raw_artifact_id: str | None
    confirmed_at: str | None
    created_at: str


@dataclass(frozen=True, slots=True)
class WritingUnitPreparation:
    id: str
    project_id: str
    context_id: str
    stage_run_id: str
    unit_id: str
    version: int
    input_hash: str
    request_sha256: str
    manifest_sha256: str
    request_artifact_id: str | None
    manifest_artifact_id: str | None
    created_at: str


@dataclass(frozen=True, slots=True)
class WritingUnitRecoveryResult:
    project_id: str
    context_id: str
    unit_id: str
    preparation_id: str
    previous_version: int
    version: int
    stage_run_id: str
    previous_stage_run_status: RunStatus
    stage_run_status: RunStatus
    finished_at: str
    artifacts: tuple[Artifact, ...]
    recovered: bool


@dataclass(frozen=True, slots=True)
class PreparationDependency:
    preparation_id: str
    project_id: str
    context_id: str
    dependency_unit_id: str
    submission_id: str
    accepted_version: int
    artifact_id: str
    sha256: str


@dataclass(frozen=True, slots=True)
class TextUnitSubmission:
    id: str
    project_id: str
    context_id: str
    preparation_id: str
    stage_run_id: str
    unit_id: str
    raw_version: int
    accepted_version: int | None
    disposition: SubmissionDisposition
    raw_sha256: str
    raw_artifact_id: str | None
    validation_report_sha256: str
    validation_report_artifact_id: str | None
    accepted_artifact_id: str | None
    citation_ledger_sha256: str | None
    citation_ledger_artifact_id: str | None
    character_count: int
    warning_count: int
    created_at: str
    accepted_at: str | None


@dataclass(frozen=True, slots=True)
class ValidationFinding:
    code: str
    severity: FindingSeverity
    message: str
    evidence: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class TextValidationReport:
    unit_id: str
    character_count: int
    disposition: SubmissionDisposition
    findings: tuple[ValidationFinding, ...]


@dataclass(frozen=True, slots=True)
class CitationOccurrence:
    id: str
    project_id: str
    submission_id: str
    unit_id: str
    ordinal: int
    raw_citation_text: str
    observed_author: str
    normalized_author: str
    year_text: str
    parsed_year: int | None
    start_offset: int
    end_offset: int
    extraction_rule_version: int


@dataclass(frozen=True, slots=True)
class ProductionSelection:
    unit_id: str
    status: ProductionUnitStatus
    submission: TextUnitSubmission | None
    accepted_artifact_id: str | None
    accepted_sha256: str | None


@dataclass(frozen=True, slots=True)
class TextProductionSet:
    project_id: str
    context_id: str
    selections: tuple[ProductionSelection, ...]
    complete: bool
    context_current: bool
    current: bool
    production_set_hash: str | None


@dataclass(frozen=True, slots=True)
class TextConsolidation:
    id: str
    project_id: str
    context_id: str
    stage_run_id: str
    version: int
    production_set_hash: str
    separator: str
    text_sha256: str
    manifest_sha256: str
    text_artifact_id: str | None
    manifest_artifact_id: str | None
    created_at: str

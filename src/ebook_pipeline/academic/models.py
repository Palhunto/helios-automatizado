from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from ebook_pipeline.core.models import Artifact


class DocumentKind(StrEnum):
    QUESTIONNAIRE = "questionnaire"
    CONSOLIDATED_ANSWERS = "consolidated_answers"
    ACADEMIC_PLAN = "academic_plan"


class AcceptanceStatus(StrEnum):
    PROCESSING = "processing"
    ACCEPTED = "accepted"
    REVIEW_REQUIRED = "review_required"
    REJECTED = "rejected"


class WordingStatus(StrEnum):
    NORMALIZED_MATCH = "normalized_match"
    REVIEW_REQUIRED = "review_required"
    REVIEW_CONFIRMED = "review_confirmed"


class EditorialMarker(StrEnum):
    CONFIRMADO = "CONFIRMADO"
    COMPLEMENTO_PROPOSTO = "COMPLEMENTO_PROPOSTO"
    PREMISSA_EDITORIAL = "PREMISSA_EDITORIAL"
    PENDENTE = "PENDENTE"
    CONFLITO = "CONFLITO"


class HeaderStatus(StrEnum):
    PRESENT = "present"
    NORMALIZED_VARIANT = "normalized_variant"
    MISSING = "missing"


@dataclass(frozen=True, slots=True)
class ObservedQuestion:
    question_number: int
    canonical_question_number: int
    canonical_text: str
    observed_text: str
    wording_status: WordingStatus


@dataclass(frozen=True, slots=True)
class Questionnaire:
    questions: tuple[ObservedQuestion, ...]


@dataclass(frozen=True, slots=True)
class EditorialBlock:
    status: EditorialMarker
    text: str
    source_order: int


@dataclass(frozen=True, slots=True)
class ConsolidatedAnswer:
    question_number: int
    blocks: tuple[EditorialBlock, ...]


@dataclass(frozen=True, slots=True)
class ConsolidatedAnswers:
    header_status: HeaderStatus
    answers: tuple[ConsolidatedAnswer, ...]


@dataclass(frozen=True, slots=True)
class AcademicDocument:
    id: str
    project_id: str
    stage_run_id: str
    document_kind: DocumentKind
    acceptance_status: AcceptanceStatus
    raw_version: int
    accepted_version: int | None
    raw_sha256: str
    raw_artifact_id: str | None
    accepted_artifact_id: str | None
    prompt_id: str
    prompt_version: int
    prompt_sha256: str
    upstream_document_id: str | None
    authorization_id: str | None
    created_at: str
    accepted_at: str | None


@dataclass(frozen=True, slots=True)
class AcademicPlanAuthorization:
    id: str
    project_id: str
    answers_document_id: str
    conflict_count: int
    authorized_at: str


@dataclass(frozen=True, slots=True)
class AcademicReviewDecision:
    id: str
    project_id: str
    questionnaire_document_id: str
    stage_run_id: str
    decision: str
    created_at: str


@dataclass(frozen=True, slots=True)
class AcceptedAcademicArtifact:
    document: AcademicDocument
    artifact: Artifact
    content: bytes


@dataclass(frozen=True, slots=True)
class CurrentWritingInputs:
    answers: AcceptedAcademicArtifact
    plan: AcceptedAcademicArtifact
    rendered_answers: bytes

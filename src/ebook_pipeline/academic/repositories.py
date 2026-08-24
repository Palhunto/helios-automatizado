from __future__ import annotations

import sqlite3
from dataclasses import replace

from ebook_pipeline.academic.models import (
    AcademicDocument,
    AcademicPlanAuthorization,
    AcademicReviewDecision,
    AcceptanceStatus,
    DocumentKind,
)
from ebook_pipeline.core.errors import ConflictError, NotFoundError
from ebook_pipeline.core.ids import new_id, utc_now
from ebook_pipeline.core.models import Artifact, StageRun
from ebook_pipeline.storage.repositories import _stage_run


def _document(row: sqlite3.Row) -> AcademicDocument:
    values = dict(row)
    values["document_kind"] = DocumentKind(values["document_kind"])
    values["acceptance_status"] = AcceptanceStatus(values["acceptance_status"])
    return AcademicDocument(**values)


def _authorization(row: sqlite3.Row) -> AcademicPlanAuthorization:
    return AcademicPlanAuthorization(**dict(row))


def _review(row: sqlite3.Row) -> AcademicReviewDecision:
    return AcademicReviewDecision(**dict(row))


class AcademicDocumentRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def add(self, document: AcademicDocument) -> None:
        try:
            self.connection.execute(
                "INSERT INTO academic_documents("
                "id, project_id, stage_run_id, document_kind, acceptance_status, raw_version, "
                "accepted_version, raw_sha256, raw_artifact_id, accepted_artifact_id, prompt_id, "
                "prompt_version, prompt_sha256, upstream_document_id, authorization_id, "
                "created_at, accepted_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    document.id,
                    document.project_id,
                    document.stage_run_id,
                    document.document_kind.value,
                    document.acceptance_status.value,
                    document.raw_version,
                    document.accepted_version,
                    document.raw_sha256,
                    document.raw_artifact_id,
                    document.accepted_artifact_id,
                    document.prompt_id,
                    document.prompt_version,
                    document.prompt_sha256,
                    document.upstream_document_id,
                    document.authorization_id,
                    document.created_at,
                    document.accepted_at,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(
                "ACADEMIC_DOCUMENT_CONFLICT", f"Could not create academic document: {exc}"
            ) from exc

    def get(self, document_id: str) -> AcademicDocument:
        row = self.connection.execute(
            "SELECT * FROM academic_documents WHERE id = ?", (document_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(
                "ACADEMIC_DOCUMENT_NOT_FOUND", f"Academic document {document_id!r} was not found"
            )
        return _document(row)

    def get_by_run(self, run_id: str) -> AcademicDocument | None:
        row = self.connection.execute(
            "SELECT * FROM academic_documents WHERE stage_run_id = ?", (run_id,)
        ).fetchone()
        return None if row is None else _document(row)

    def find_by_input(
        self, project_id: str, kind: DocumentKind, input_hash: str
    ) -> tuple[AcademicDocument, StageRun] | None:
        row = self.connection.execute(
            "SELECT d.* FROM academic_documents d "
            "JOIN stage_runs r ON r.id = d.stage_run_id "
            "WHERE d.project_id = ? AND d.document_kind = ? AND r.input_hash = ? "
            "ORDER BY d.raw_version DESC LIMIT 1",
            (project_id, kind.value, input_hash),
        ).fetchone()
        if row is None:
            return None
        document = _document(row)
        run_row = self.connection.execute(
            "SELECT * FROM stage_runs WHERE id = ?", (document.stage_run_id,)
        ).fetchone()
        assert run_row is not None
        return document, _stage_run(run_row)

    def latest(self, project_id: str, kind: DocumentKind) -> AcademicDocument | None:
        row = self.connection.execute(
            "SELECT * FROM academic_documents WHERE project_id = ? AND document_kind = ? "
            "ORDER BY raw_version DESC LIMIT 1",
            (project_id, kind.value),
        ).fetchone()
        return None if row is None else _document(row)

    def current_accepted(self, project_id: str, kind: DocumentKind) -> AcademicDocument | None:
        row = self.connection.execute(
            "SELECT * FROM academic_documents WHERE project_id = ? AND document_kind = ? "
            "AND acceptance_status = 'accepted' ORDER BY accepted_version DESC LIMIT 1",
            (project_id, kind.value),
        ).fetchone()
        return None if row is None else _document(row)

    def by_raw_version(
        self, project_id: str, kind: DocumentKind, version: int
    ) -> AcademicDocument | None:
        row = self.connection.execute(
            "SELECT * FROM academic_documents WHERE project_id = ? AND document_kind = ? "
            "AND raw_version = ?",
            (project_id, kind.value, version),
        ).fetchone()
        return None if row is None else _document(row)

    def by_accepted_version(
        self, project_id: str, kind: DocumentKind, version: int
    ) -> AcademicDocument | None:
        row = self.connection.execute(
            "SELECT * FROM academic_documents WHERE project_id = ? AND document_kind = ? "
            "AND accepted_version = ?",
            (project_id, kind.value, version),
        ).fetchone()
        return None if row is None else _document(row)

    def list_for_project(self, project_id: str) -> list[AcademicDocument]:
        rows = self.connection.execute(
            "SELECT * FROM academic_documents WHERE project_id = ? "
            "ORDER BY created_at, raw_version, id",
            (project_id,),
        ).fetchall()
        return [_document(row) for row in rows]

    def set_raw_artifact(self, document: AcademicDocument, artifact_id: str) -> AcademicDocument:
        updated = replace(document, raw_artifact_id=artifact_id)
        cursor = self.connection.execute(
            "UPDATE academic_documents SET raw_artifact_id = ? "
            "WHERE id = ? AND raw_artifact_id IS NULL",
            (artifact_id, document.id),
        )
        if cursor.rowcount != 1:
            current = self.get(document.id)
            if current.raw_artifact_id == artifact_id:
                return current
            raise ConflictError(
                "ACADEMIC_DOCUMENT_CONCURRENT_UPDATE", "Raw artifact changed concurrently"
            )
        return updated

    def set_disposition(
        self, document: AcademicDocument, status: AcceptanceStatus
    ) -> AcademicDocument:
        if status not in {AcceptanceStatus.REVIEW_REQUIRED, AcceptanceStatus.REJECTED}:
            raise ConflictError(
                "ACADEMIC_DISPOSITION_INVALID",
                f"Processing document cannot be set to {status.value}",
            )
        updated = replace(document, acceptance_status=status)
        cursor = self.connection.execute(
            "UPDATE academic_documents SET acceptance_status = ? "
            "WHERE id = ? AND acceptance_status = 'processing'",
            (status.value, document.id),
        )
        if cursor.rowcount != 1:
            raise ConflictError(
                "ACADEMIC_DOCUMENT_CONCURRENT_UPDATE", "Document disposition changed concurrently"
            )
        return updated

    def accept(
        self, document: AcademicDocument, artifact_id: str, accepted_version: int
    ) -> AcademicDocument:
        timestamp = utc_now()
        cursor = self.connection.execute(
            "UPDATE academic_documents SET acceptance_status = 'accepted', accepted_version = ?, "
            "accepted_artifact_id = ?, accepted_at = ? WHERE id = ? "
            "AND acceptance_status IN ('processing', 'review_required')",
            (accepted_version, artifact_id, timestamp, document.id),
        )
        if cursor.rowcount != 1:
            current = self.get(document.id)
            if (
                current.acceptance_status is AcceptanceStatus.ACCEPTED
                and current.accepted_artifact_id == artifact_id
            ):
                return current
            raise ConflictError(
                "ACADEMIC_DOCUMENT_CONCURRENT_UPDATE", "Document acceptance changed concurrently"
            )
        return replace(
            document,
            acceptance_status=AcceptanceStatus.ACCEPTED,
            accepted_version=accepted_version,
            accepted_artifact_id=artifact_id,
            accepted_at=timestamp,
        )


class AuthorizationRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def get_for_answers(self, answers_document_id: str) -> AcademicPlanAuthorization | None:
        row = self.connection.execute(
            "SELECT * FROM academic_plan_authorizations WHERE answers_document_id = ?",
            (answers_document_id,),
        ).fetchone()
        return None if row is None else _authorization(row)

    def add(self, project_id: str, answers_document_id: str) -> AcademicPlanAuthorization:
        existing = self.get_for_answers(answers_document_id)
        if existing is not None:
            return existing
        authorization = AcademicPlanAuthorization(
            id=new_id(),
            project_id=project_id,
            answers_document_id=answers_document_id,
            conflict_count=0,
            authorized_at=utc_now(),
        )
        try:
            self.connection.execute(
                "INSERT INTO academic_plan_authorizations("
                "id, project_id, answers_document_id, conflict_count, authorized_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    authorization.id,
                    authorization.project_id,
                    authorization.answers_document_id,
                    authorization.conflict_count,
                    authorization.authorized_at,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(
                "ACADEMIC_AUTHORIZATION_CONFLICT", f"Could not authorize plan: {exc}"
            ) from exc
        return authorization


class ReviewDecisionRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def get_for_document(self, document_id: str) -> AcademicReviewDecision | None:
        row = self.connection.execute(
            "SELECT * FROM academic_review_decisions WHERE questionnaire_document_id = ?",
            (document_id,),
        ).fetchone()
        return None if row is None else _review(row)

    def add(self, project_id: str, document_id: str, stage_run_id: str) -> AcademicReviewDecision:
        existing = self.get_for_document(document_id)
        if existing is not None:
            return existing
        decision = AcademicReviewDecision(
            id=new_id(),
            project_id=project_id,
            questionnaire_document_id=document_id,
            stage_run_id=stage_run_id,
            decision="confirm_observed_questionnaire",
            created_at=utc_now(),
        )
        self.connection.execute(
            "INSERT INTO academic_review_decisions("
            "id, project_id, questionnaire_document_id, stage_run_id, decision, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                decision.id,
                decision.project_id,
                decision.questionnaire_document_id,
                decision.stage_run_id,
                decision.decision,
                decision.created_at,
            ),
        )
        return decision


class AcademicArtifactRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def get(self, artifact_id: str) -> Artifact:
        row = self.connection.execute(
            "SELECT * FROM artifacts WHERE id = ?", (artifact_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("ARTIFACT_NOT_FOUND", f"Artifact {artifact_id!r} was not found")
        return Artifact(**dict(row))

    def for_run(self, run_id: str) -> list[Artifact]:
        rows = self.connection.execute(
            "SELECT * FROM artifacts WHERE stage_run_id = ? ORDER BY artifact_type, version",
            (run_id,),
        ).fetchall()
        return [Artifact(**dict(row)) for row in rows]

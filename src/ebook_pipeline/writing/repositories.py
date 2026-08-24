from __future__ import annotations

import sqlite3
from dataclasses import replace

from ebook_pipeline.core.errors import ConflictError, NotFoundError
from ebook_pipeline.core.models import Artifact
from ebook_pipeline.writing.models import (
    CitationOccurrence,
    PreparationDependency,
    SubmissionDisposition,
    TextConsolidation,
    TextUnitSubmission,
    WritingAcknowledgement,
    WritingContext,
    WritingUnitPreparation,
)


def _context(row: sqlite3.Row) -> WritingContext:
    return WritingContext(**dict(row))


def _acknowledgement(row: sqlite3.Row) -> WritingAcknowledgement:
    return WritingAcknowledgement(**dict(row))


def _preparation(row: sqlite3.Row) -> WritingUnitPreparation:
    return WritingUnitPreparation(**dict(row))


def _dependency(row: sqlite3.Row) -> PreparationDependency:
    return PreparationDependency(**dict(row))


def _submission(row: sqlite3.Row) -> TextUnitSubmission:
    values = dict(row)
    values["disposition"] = SubmissionDisposition(values["disposition"])
    return TextUnitSubmission(**values)


def _citation(row: sqlite3.Row) -> CitationOccurrence:
    return CitationOccurrence(**dict(row))


def _consolidation(row: sqlite3.Row) -> TextConsolidation:
    return TextConsolidation(**dict(row))


class WritingArtifactRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def get(self, artifact_id: str) -> Artifact:
        row = self.connection.execute(
            "SELECT * FROM artifacts WHERE id = ?", (artifact_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("ARTIFACT_NOT_FOUND", f"Artifact {artifact_id!r} was not found")
        return Artifact(**dict(row))


class WritingContextRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def add(self, context: WritingContext) -> None:
        try:
            self.connection.execute(
                "INSERT INTO writing_contexts("
                "id, project_id, stage_run_id, version, input_hash, answers_document_id, "
                "answers_artifact_id, answers_sha256, plan_document_id, plan_artifact_id, "
                "plan_sha256, writing_prompt_id, writing_prompt_version, "
                "writing_prompt_sha256, contract_id, contract_version, contract_sha256, "
                "request_prompt_id, request_prompt_version, request_prompt_sha256, "
                "package_sha256, manifest_sha256, package_artifact_id, manifest_artifact_id, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "?, ?, ?, ?, ?, ?, ?)",
                (
                    context.id,
                    context.project_id,
                    context.stage_run_id,
                    context.version,
                    context.input_hash,
                    context.answers_document_id,
                    context.answers_artifact_id,
                    context.answers_sha256,
                    context.plan_document_id,
                    context.plan_artifact_id,
                    context.plan_sha256,
                    context.writing_prompt_id,
                    context.writing_prompt_version,
                    context.writing_prompt_sha256,
                    context.contract_id,
                    context.contract_version,
                    context.contract_sha256,
                    context.request_prompt_id,
                    context.request_prompt_version,
                    context.request_prompt_sha256,
                    context.package_sha256,
                    context.manifest_sha256,
                    context.package_artifact_id,
                    context.manifest_artifact_id,
                    context.created_at,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(
                "WRITING_CONTEXT_CONFLICT", f"Could not create writing context: {exc}"
            ) from exc

    def get(self, context_id: str) -> WritingContext:
        row = self.connection.execute(
            "SELECT * FROM writing_contexts WHERE id = ?", (context_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(
                "WRITING_CONTEXT_NOT_FOUND", f"Writing context {context_id!r} was not found"
            )
        return _context(row)

    def get_by_run(self, run_id: str) -> WritingContext | None:
        row = self.connection.execute(
            "SELECT * FROM writing_contexts WHERE stage_run_id = ?", (run_id,)
        ).fetchone()
        return None if row is None else _context(row)

    def latest(self, project_id: str) -> WritingContext | None:
        row = self.connection.execute(
            "SELECT * FROM writing_contexts WHERE project_id = ? ORDER BY version DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        return None if row is None else _context(row)

    def by_version(self, project_id: str, version: int) -> WritingContext | None:
        row = self.connection.execute(
            "SELECT * FROM writing_contexts WHERE project_id = ? AND version = ?",
            (project_id, version),
        ).fetchone()
        return None if row is None else _context(row)

    def by_input(self, project_id: str, input_hash: str) -> WritingContext | None:
        row = self.connection.execute(
            "SELECT * FROM writing_contexts WHERE project_id = ? AND input_hash = ?",
            (project_id, input_hash),
        ).fetchone()
        return None if row is None else _context(row)

    def list_for_project(self, project_id: str) -> list[WritingContext]:
        rows = self.connection.execute(
            "SELECT * FROM writing_contexts WHERE project_id = ? ORDER BY version", (project_id,)
        ).fetchall()
        return [_context(row) for row in rows]

    def set_artifacts(
        self, context: WritingContext, package_artifact_id: str, manifest_artifact_id: str
    ) -> WritingContext:
        cursor = self.connection.execute(
            "UPDATE writing_contexts SET package_artifact_id = ?, manifest_artifact_id = ? "
            "WHERE id = ? AND package_artifact_id IS NULL AND manifest_artifact_id IS NULL",
            (package_artifact_id, manifest_artifact_id, context.id),
        )
        if cursor.rowcount != 1:
            current = self.get(context.id)
            if (
                current.package_artifact_id == package_artifact_id
                and current.manifest_artifact_id == manifest_artifact_id
            ):
                return current
            raise ConflictError(
                "WRITING_CONTEXT_CONCURRENT_UPDATE", "Writing context artifacts changed"
            )
        return replace(
            context,
            package_artifact_id=package_artifact_id,
            manifest_artifact_id=manifest_artifact_id,
        )


class AcknowledgementRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def add(self, item: WritingAcknowledgement) -> None:
        try:
            self.connection.execute(
                "INSERT INTO writing_acknowledgements("
                "id, project_id, context_id, stage_run_id, raw_version, raw_sha256, "
                "raw_artifact_id, confirmed_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.id,
                    item.project_id,
                    item.context_id,
                    item.stage_run_id,
                    item.raw_version,
                    item.raw_sha256,
                    item.raw_artifact_id,
                    item.confirmed_at,
                    item.created_at,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(
                "WRITING_ACKNOWLEDGEMENT_CONFLICT", f"Could not create acknowledgement: {exc}"
            ) from exc

    def get(self, acknowledgement_id: str) -> WritingAcknowledgement:
        row = self.connection.execute(
            "SELECT * FROM writing_acknowledgements WHERE id = ?", (acknowledgement_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(
                "WRITING_ACKNOWLEDGEMENT_NOT_FOUND",
                f"Writing acknowledgement {acknowledgement_id!r} was not found",
            )
        return _acknowledgement(row)

    def get_by_run(self, run_id: str) -> WritingAcknowledgement | None:
        row = self.connection.execute(
            "SELECT * FROM writing_acknowledgements WHERE stage_run_id = ?", (run_id,)
        ).fetchone()
        return None if row is None else _acknowledgement(row)

    def by_raw_version(self, context_id: str, raw_version: int) -> WritingAcknowledgement | None:
        row = self.connection.execute(
            "SELECT * FROM writing_acknowledgements WHERE context_id = ? AND raw_version = ?",
            (context_id, raw_version),
        ).fetchone()
        return None if row is None else _acknowledgement(row)

    def by_hash(self, context_id: str, raw_sha256: str) -> WritingAcknowledgement | None:
        row = self.connection.execute(
            "SELECT * FROM writing_acknowledgements WHERE context_id = ? AND raw_sha256 = ?",
            (context_id, raw_sha256),
        ).fetchone()
        return None if row is None else _acknowledgement(row)

    def latest(self, context_id: str) -> WritingAcknowledgement | None:
        row = self.connection.execute(
            "SELECT * FROM writing_acknowledgements WHERE context_id = ? "
            "ORDER BY raw_version DESC LIMIT 1",
            (context_id,),
        ).fetchone()
        return None if row is None else _acknowledgement(row)

    def confirmed(self, context_id: str) -> WritingAcknowledgement | None:
        row = self.connection.execute(
            "SELECT * FROM writing_acknowledgements WHERE context_id = ? "
            "AND confirmed_at IS NOT NULL ORDER BY raw_version DESC LIMIT 1",
            (context_id,),
        ).fetchone()
        return None if row is None else _acknowledgement(row)

    def set_artifact(
        self, item: WritingAcknowledgement, artifact_id: str
    ) -> WritingAcknowledgement:
        cursor = self.connection.execute(
            "UPDATE writing_acknowledgements SET raw_artifact_id = ? "
            "WHERE id = ? AND raw_artifact_id IS NULL",
            (artifact_id, item.id),
        )
        if cursor.rowcount != 1:
            current = self.get(item.id)
            if current.raw_artifact_id == artifact_id:
                return current
            raise ConflictError(
                "WRITING_ACKNOWLEDGEMENT_CONCURRENT_UPDATE",
                "Acknowledgement artifact changed",
            )
        return replace(item, raw_artifact_id=artifact_id)

    def confirm(self, item: WritingAcknowledgement, confirmed_at: str) -> WritingAcknowledgement:
        cursor = self.connection.execute(
            "UPDATE writing_acknowledgements SET confirmed_at = ? "
            "WHERE id = ? AND confirmed_at IS NULL AND raw_artifact_id IS NOT NULL",
            (confirmed_at, item.id),
        )
        if cursor.rowcount != 1:
            current = self.get(item.id)
            if current.confirmed_at is not None:
                return current
            raise ConflictError(
                "WRITING_ACKNOWLEDGEMENT_CONCURRENT_UPDATE", "Acknowledgement was not confirmable"
            )
        return replace(item, confirmed_at=confirmed_at)


class PreparationRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def add(self, item: WritingUnitPreparation) -> None:
        try:
            self.connection.execute(
                "INSERT INTO writing_unit_preparations("
                "id, project_id, context_id, stage_run_id, unit_id, version, input_hash, "
                "request_sha256, manifest_sha256, request_artifact_id, manifest_artifact_id, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.id,
                    item.project_id,
                    item.context_id,
                    item.stage_run_id,
                    item.unit_id,
                    item.version,
                    item.input_hash,
                    item.request_sha256,
                    item.manifest_sha256,
                    item.request_artifact_id,
                    item.manifest_artifact_id,
                    item.created_at,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(
                "WRITING_PREPARATION_CONFLICT", f"Could not create preparation: {exc}"
            ) from exc

    def get(self, preparation_id: str) -> WritingUnitPreparation:
        row = self.connection.execute(
            "SELECT * FROM writing_unit_preparations WHERE id = ?", (preparation_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(
                "WRITING_PREPARATION_NOT_FOUND",
                f"Writing preparation {preparation_id!r} was not found",
            )
        return _preparation(row)

    def get_by_run(self, run_id: str) -> WritingUnitPreparation | None:
        row = self.connection.execute(
            "SELECT * FROM writing_unit_preparations WHERE stage_run_id = ?", (run_id,)
        ).fetchone()
        return None if row is None else _preparation(row)

    def latest(self, context_id: str, unit_id: str) -> WritingUnitPreparation | None:
        row = self.connection.execute(
            "SELECT * FROM writing_unit_preparations WHERE context_id = ? AND unit_id = ? "
            "ORDER BY version DESC LIMIT 1",
            (context_id, unit_id),
        ).fetchone()
        return None if row is None else _preparation(row)

    def latest_for_project_unit(
        self, project_id: str, unit_id: str
    ) -> WritingUnitPreparation | None:
        row = self.connection.execute(
            "SELECT * FROM writing_unit_preparations WHERE project_id = ? AND unit_id = ? "
            "ORDER BY version DESC, created_at DESC, id DESC LIMIT 1",
            (project_id, unit_id),
        ).fetchone()
        return None if row is None else _preparation(row)

    def at_project_unit_version(
        self, project_id: str, unit_id: str, version: int
    ) -> list[WritingUnitPreparation]:
        rows = self.connection.execute(
            "SELECT * FROM writing_unit_preparations WHERE project_id = ? AND unit_id = ? "
            "AND version = ? ORDER BY created_at, id",
            (project_id, unit_id, version),
        ).fetchall()
        return [_preparation(row) for row in rows]

    def has_later_preparation(self, item: WritingUnitPreparation) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM writing_unit_preparations WHERE project_id = ? AND unit_id = ? "
            "AND (created_at > ? OR (created_at = ? AND id > ?)) LIMIT 1",
            (item.project_id, item.unit_id, item.created_at, item.created_at, item.id),
        ).fetchone()
        return row is not None

    def reallocate_running_version(
        self, item: WritingUnitPreparation, version: int
    ) -> WritingUnitPreparation:
        if version <= item.version:
            raise ConflictError(
                "WRITING_PREPARATION_VERSION_REALLOCATION_INVALID",
                "An incomplete preparation can only move to a newer version",
            )
        cursor = self.connection.execute(
            "UPDATE writing_unit_preparations SET version = ? WHERE id = ? AND version = ? "
            "AND request_artifact_id IS NULL AND manifest_artifact_id IS NULL "
            "AND NOT EXISTS (SELECT 1 FROM artifacts WHERE stage_run_id = ?)",
            (version, item.id, item.version, item.stage_run_id),
        )
        if cursor.rowcount != 1:
            raise ConflictError(
                "WRITING_PREPARATION_VERSION_REALLOCATION_CONFLICT",
                "Incomplete preparation changed before version reallocation",
            )
        return replace(item, version=version)

    def by_input(
        self, context_id: str, unit_id: str, input_hash: str
    ) -> WritingUnitPreparation | None:
        row = self.connection.execute(
            "SELECT * FROM writing_unit_preparations WHERE context_id = ? AND unit_id = ? "
            "AND input_hash = ? ORDER BY version DESC LIMIT 1",
            (context_id, unit_id, input_hash),
        ).fetchone()
        return None if row is None else _preparation(row)

    def list_for_context(self, context_id: str) -> list[WritingUnitPreparation]:
        rows = self.connection.execute(
            "SELECT * FROM writing_unit_preparations WHERE context_id = ? "
            "ORDER BY unit_id, version",
            (context_id,),
        ).fetchall()
        return [_preparation(row) for row in rows]

    def set_artifacts(
        self, item: WritingUnitPreparation, request_id: str, manifest_id: str
    ) -> WritingUnitPreparation:
        cursor = self.connection.execute(
            "UPDATE writing_unit_preparations SET request_artifact_id = ?, "
            "manifest_artifact_id = ? WHERE id = ? AND request_artifact_id IS NULL "
            "AND manifest_artifact_id IS NULL",
            (request_id, manifest_id, item.id),
        )
        if cursor.rowcount != 1:
            current = self.get(item.id)
            if (
                current.request_artifact_id == request_id
                and current.manifest_artifact_id == manifest_id
            ):
                return current
            raise ConflictError(
                "WRITING_PREPARATION_CONCURRENT_UPDATE", "Preparation artifacts changed"
            )
        return replace(item, request_artifact_id=request_id, manifest_artifact_id=manifest_id)

    def add_dependencies(self, dependencies: tuple[PreparationDependency, ...]) -> None:
        for item in dependencies:
            try:
                self.connection.execute(
                    "INSERT INTO writing_preparation_dependencies("
                    "preparation_id, project_id, context_id, dependency_unit_id, submission_id, "
                    "accepted_version, artifact_id, sha256) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        item.preparation_id,
                        item.project_id,
                        item.context_id,
                        item.dependency_unit_id,
                        item.submission_id,
                        item.accepted_version,
                        item.artifact_id,
                        item.sha256,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError(
                    "WRITING_DEPENDENCY_CONFLICT",
                    f"Could not record preparation dependency: {exc}",
                ) from exc

    def dependencies(self, preparation_id: str) -> tuple[PreparationDependency, ...]:
        rows = self.connection.execute(
            "SELECT * FROM writing_preparation_dependencies WHERE preparation_id = ? "
            "ORDER BY dependency_unit_id",
            (preparation_id,),
        ).fetchall()
        return tuple(_dependency(row) for row in rows)


class SubmissionRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def add(self, item: TextUnitSubmission) -> None:
        try:
            self.connection.execute(
                "INSERT INTO text_unit_submissions("
                "id, project_id, context_id, preparation_id, stage_run_id, unit_id, raw_version, "
                "accepted_version, disposition, raw_sha256, raw_artifact_id, "
                "validation_report_sha256, validation_report_artifact_id, accepted_artifact_id, "
                "citation_ledger_sha256, citation_ledger_artifact_id, character_count, "
                "warning_count, created_at, accepted_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.id,
                    item.project_id,
                    item.context_id,
                    item.preparation_id,
                    item.stage_run_id,
                    item.unit_id,
                    item.raw_version,
                    item.accepted_version,
                    item.disposition.value,
                    item.raw_sha256,
                    item.raw_artifact_id,
                    item.validation_report_sha256,
                    item.validation_report_artifact_id,
                    item.accepted_artifact_id,
                    item.citation_ledger_sha256,
                    item.citation_ledger_artifact_id,
                    item.character_count,
                    item.warning_count,
                    item.created_at,
                    item.accepted_at,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(
                "WRITING_SUBMISSION_CONFLICT", f"Could not create submission: {exc}"
            ) from exc

    def get(self, submission_id: str) -> TextUnitSubmission:
        row = self.connection.execute(
            "SELECT * FROM text_unit_submissions WHERE id = ?", (submission_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(
                "WRITING_SUBMISSION_NOT_FOUND", f"Submission {submission_id!r} was not found"
            )
        return _submission(row)

    def get_by_run(self, run_id: str) -> TextUnitSubmission | None:
        row = self.connection.execute(
            "SELECT * FROM text_unit_submissions WHERE stage_run_id = ?", (run_id,)
        ).fetchone()
        return None if row is None else _submission(row)

    def by_preparation_hash(
        self, preparation_id: str, raw_sha256: str
    ) -> TextUnitSubmission | None:
        row = self.connection.execute(
            "SELECT * FROM text_unit_submissions WHERE preparation_id = ? AND raw_sha256 = ?",
            (preparation_id, raw_sha256),
        ).fetchone()
        return None if row is None else _submission(row)

    def by_raw_version(
        self, context_id: str, unit_id: str, version: int
    ) -> TextUnitSubmission | None:
        row = self.connection.execute(
            "SELECT * FROM text_unit_submissions WHERE context_id = ? AND unit_id = ? "
            "AND raw_version = ?",
            (context_id, unit_id, version),
        ).fetchone()
        return None if row is None else _submission(row)

    def by_accepted_version(
        self, context_id: str, unit_id: str, version: int
    ) -> TextUnitSubmission | None:
        row = self.connection.execute(
            "SELECT * FROM text_unit_submissions WHERE context_id = ? AND unit_id = ? "
            "AND accepted_version = ?",
            (context_id, unit_id, version),
        ).fetchone()
        return None if row is None else _submission(row)

    def latest(self, context_id: str, unit_id: str) -> TextUnitSubmission | None:
        row = self.connection.execute(
            "SELECT * FROM text_unit_submissions WHERE context_id = ? AND unit_id = ? "
            "ORDER BY raw_version DESC LIMIT 1",
            (context_id, unit_id),
        ).fetchone()
        return None if row is None else _submission(row)

    def latest_for_project_unit(
        self, project_id: str, unit_id: str
    ) -> TextUnitSubmission | None:
        row = self.connection.execute(
            "SELECT * FROM text_unit_submissions WHERE project_id = ? AND unit_id = ? "
            "ORDER BY raw_version DESC, created_at DESC, id DESC LIMIT 1",
            (project_id, unit_id),
        ).fetchone()
        return None if row is None else _submission(row)

    def raw_version_used_by_other(self, item: TextUnitSubmission) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM text_unit_submissions WHERE project_id = ? AND unit_id = ? "
            "AND raw_version = ? AND id != ? LIMIT 1",
            (item.project_id, item.unit_id, item.raw_version, item.id),
        ).fetchone()
        return row is not None

    def reallocate_processing_raw_version(
        self, item: TextUnitSubmission, raw_version: int
    ) -> TextUnitSubmission:
        if raw_version <= item.raw_version:
            raise ConflictError(
                "WRITING_RAW_VERSION_REALLOCATION_INVALID",
                "A processing submission can only move to a newer raw version",
            )
        cursor = self.connection.execute(
            "UPDATE text_unit_submissions SET raw_version = ? WHERE id = ? "
            "AND raw_version = ? AND disposition = 'processing' "
            "AND raw_artifact_id IS NULL AND validation_report_artifact_id IS NULL "
            "AND accepted_artifact_id IS NULL AND citation_ledger_artifact_id IS NULL",
            (raw_version, item.id, item.raw_version),
        )
        if cursor.rowcount != 1:
            raise ConflictError(
                "WRITING_RAW_VERSION_REALLOCATION_CONFLICT",
                "Processing submission changed before raw version reallocation",
            )
        return replace(item, raw_version=raw_version)

    def list_for_context(self, context_id: str) -> list[TextUnitSubmission]:
        rows = self.connection.execute(
            "SELECT * FROM text_unit_submissions WHERE context_id = ? "
            "ORDER BY unit_id, raw_version",
            (context_id,),
        ).fetchall()
        return [_submission(row) for row in rows]

    def accepted_for_unit(self, context_id: str, unit_id: str) -> list[TextUnitSubmission]:
        rows = self.connection.execute(
            "SELECT * FROM text_unit_submissions WHERE context_id = ? AND unit_id = ? "
            "AND disposition = 'accepted' ORDER BY accepted_version DESC",
            (context_id, unit_id),
        ).fetchall()
        return [_submission(row) for row in rows]

    def materialize(
        self,
        item: TextUnitSubmission,
        *,
        disposition: SubmissionDisposition,
        raw_artifact_id: str,
        report_artifact_id: str,
        accepted_artifact_id: str | None,
        citation_ledger_sha256: str | None,
        citation_ledger_artifact_id: str | None,
        accepted_version: int | None,
        accepted_at: str | None,
    ) -> TextUnitSubmission:
        cursor = self.connection.execute(
            "UPDATE text_unit_submissions SET disposition = ?, raw_artifact_id = ?, "
            "validation_report_artifact_id = ?, accepted_artifact_id = ?, "
            "citation_ledger_sha256 = ?, citation_ledger_artifact_id = ?, "
            "accepted_version = ?, accepted_at = ? WHERE id = ? AND disposition = 'processing'",
            (
                disposition.value,
                raw_artifact_id,
                report_artifact_id,
                accepted_artifact_id,
                citation_ledger_sha256,
                citation_ledger_artifact_id,
                accepted_version,
                accepted_at,
                item.id,
            ),
        )
        if cursor.rowcount != 1:
            current = self.get(item.id)
            if current.disposition is disposition:
                return current
            raise ConflictError(
                "WRITING_SUBMISSION_CONCURRENT_UPDATE", "Submission disposition changed"
            )
        return replace(
            item,
            disposition=disposition,
            raw_artifact_id=raw_artifact_id,
            validation_report_artifact_id=report_artifact_id,
            accepted_artifact_id=accepted_artifact_id,
            citation_ledger_sha256=citation_ledger_sha256,
            citation_ledger_artifact_id=citation_ledger_artifact_id,
            accepted_version=accepted_version,
            accepted_at=accepted_at,
        )

    def accept_reviewed(
        self,
        item: TextUnitSubmission,
        artifact_id: str,
        citation_ledger_sha256: str,
        citation_ledger_artifact_id: str,
        accepted_version: int,
        accepted_at: str,
    ) -> TextUnitSubmission:
        cursor = self.connection.execute(
            "UPDATE text_unit_submissions SET disposition = 'accepted', accepted_artifact_id = ?, "
            "citation_ledger_sha256 = ?, citation_ledger_artifact_id = ?, "
            "accepted_version = ?, accepted_at = ? WHERE id = ? "
            "AND disposition = 'review_required'",
            (
                artifact_id,
                citation_ledger_sha256,
                citation_ledger_artifact_id,
                accepted_version,
                accepted_at,
                item.id,
            ),
        )
        if cursor.rowcount != 1:
            current = self.get(item.id)
            if current.disposition is SubmissionDisposition.ACCEPTED:
                return current
            raise ConflictError(
                "WRITING_SUBMISSION_NOT_REVIEWABLE", "Submission cannot be confirmed"
            )
        return replace(
            item,
            disposition=SubmissionDisposition.ACCEPTED,
            accepted_artifact_id=artifact_id,
            citation_ledger_sha256=citation_ledger_sha256,
            citation_ledger_artifact_id=citation_ledger_artifact_id,
            accepted_version=accepted_version,
            accepted_at=accepted_at,
        )


class CitationRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def add_all(self, occurrences: tuple[CitationOccurrence, ...]) -> None:
        for item in occurrences:
            self.connection.execute(
                "INSERT OR IGNORE INTO citation_occurrences("
                "id, project_id, submission_id, unit_id, ordinal, raw_citation_text, "
                "observed_author, normalized_author, year_text, parsed_year, start_offset, "
                "end_offset, extraction_rule_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "?, ?, ?)",
                (
                    item.id,
                    item.project_id,
                    item.submission_id,
                    item.unit_id,
                    item.ordinal,
                    item.raw_citation_text,
                    item.observed_author,
                    item.normalized_author,
                    item.year_text,
                    item.parsed_year,
                    item.start_offset,
                    item.end_offset,
                    item.extraction_rule_version,
                ),
            )

    def for_submission(self, submission_id: str) -> tuple[CitationOccurrence, ...]:
        rows = self.connection.execute(
            "SELECT * FROM citation_occurrences WHERE submission_id = ? ORDER BY ordinal",
            (submission_id,),
        ).fetchall()
        return tuple(_citation(row) for row in rows)


class ConsolidationRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def add(self, item: TextConsolidation) -> None:
        try:
            self.connection.execute(
                "INSERT INTO text_consolidations("
                "id, project_id, context_id, stage_run_id, version, production_set_hash, "
                "separator, text_sha256, manifest_sha256, text_artifact_id, "
                "manifest_artifact_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.id,
                    item.project_id,
                    item.context_id,
                    item.stage_run_id,
                    item.version,
                    item.production_set_hash,
                    item.separator,
                    item.text_sha256,
                    item.manifest_sha256,
                    item.text_artifact_id,
                    item.manifest_artifact_id,
                    item.created_at,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(
                "TEXT_CONSOLIDATION_CONFLICT", f"Could not create consolidation: {exc}"
            ) from exc

    def get(self, consolidation_id: str) -> TextConsolidation:
        row = self.connection.execute(
            "SELECT * FROM text_consolidations WHERE id = ?", (consolidation_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(
                "TEXT_CONSOLIDATION_NOT_FOUND",
                f"Text consolidation {consolidation_id!r} was not found",
            )
        return _consolidation(row)

    def get_by_run(self, run_id: str) -> TextConsolidation | None:
        row = self.connection.execute(
            "SELECT * FROM text_consolidations WHERE stage_run_id = ?", (run_id,)
        ).fetchone()
        return None if row is None else _consolidation(row)

    def by_set_hash(self, context_id: str, set_hash: str) -> TextConsolidation | None:
        row = self.connection.execute(
            "SELECT * FROM text_consolidations WHERE context_id = ? AND production_set_hash = ?",
            (context_id, set_hash),
        ).fetchone()
        return None if row is None else _consolidation(row)

    def latest(self, project_id: str) -> TextConsolidation | None:
        row = self.connection.execute(
            "SELECT * FROM text_consolidations WHERE project_id = ? ORDER BY version DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        return None if row is None else _consolidation(row)

    def set_artifacts(
        self, item: TextConsolidation, text_artifact_id: str, manifest_artifact_id: str
    ) -> TextConsolidation:
        cursor = self.connection.execute(
            "UPDATE text_consolidations SET text_artifact_id = ?, manifest_artifact_id = ? "
            "WHERE id = ? AND text_artifact_id IS NULL AND manifest_artifact_id IS NULL",
            (text_artifact_id, manifest_artifact_id, item.id),
        )
        if cursor.rowcount != 1:
            current = self.get(item.id)
            if (
                current.text_artifact_id == text_artifact_id
                and current.manifest_artifact_id == manifest_artifact_id
            ):
                return current
            raise ConflictError(
                "TEXT_CONSOLIDATION_CONCURRENT_UPDATE", "Consolidation artifacts changed"
            )
        return replace(
            item,
            text_artifact_id=text_artifact_id,
            manifest_artifact_id=manifest_artifact_id,
        )

    def add_member(
        self,
        *,
        consolidation: TextConsolidation,
        unit_id: str,
        unit_order: int,
        submission: TextUnitSubmission,
        artifact_id: str,
        sha256: str,
    ) -> None:
        assert submission.accepted_version is not None
        self.connection.execute(
            "INSERT INTO text_consolidation_members("
            "consolidation_id, project_id, context_id, unit_id, unit_order, submission_id, "
            "accepted_version, artifact_id, sha256) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                consolidation.id,
                consolidation.project_id,
                consolidation.context_id,
                unit_id,
                unit_order,
                submission.id,
                submission.accepted_version,
                artifact_id,
                sha256,
            ),
        )

    def members(self, consolidation_id: str) -> list[sqlite3.Row]:
        return self.connection.execute(
            "SELECT * FROM text_consolidation_members WHERE consolidation_id = ? "
            "ORDER BY unit_order",
            (consolidation_id,),
        ).fetchall()

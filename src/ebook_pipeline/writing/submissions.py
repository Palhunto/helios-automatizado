from __future__ import annotations

import json
import sqlite3

from ebook_pipeline.config import AppConfig
from ebook_pipeline.core.errors import ConflictError, IntegrityError, NotFoundError
from ebook_pipeline.core.hashing import canonical_hash, sha256_bytes
from ebook_pipeline.core.ids import new_id, utc_now
from ebook_pipeline.core.models import RunStatus
from ebook_pipeline.storage.artifacts import ArtifactStore
from ebook_pipeline.storage.database import Database
from ebook_pipeline.storage.repositories import ProjectRepository, StageRunRepository
from ebook_pipeline.writing.citations import citation_ledger_bytes, extract_citations
from ebook_pipeline.writing.context import WritingContextManager
from ebook_pipeline.writing.models import (
    FindingSeverity,
    SubmissionDisposition,
    TextUnitSubmission,
)
from ebook_pipeline.writing.persistence import FaultHook, WritingPersistence
from ebook_pipeline.writing.preparation import TEXT_STAGE, WritingPreparationManager
from ebook_pipeline.writing.rendering import validation_report_bytes
from ebook_pipeline.writing.repositories import (
    CitationRepository,
    PreparationRepository,
    SubmissionRepository,
)
from ebook_pipeline.writing.validators import validate_submission


class WritingSubmissionManager:
    def __init__(
        self,
        config: AppConfig,
        database: Database,
        store: ArtifactStore,
        contexts: WritingContextManager,
        preparations: WritingPreparationManager,
        fault_hook: FaultHook | None = None,
    ) -> None:
        self.config = config
        self.database = database
        self.store = store
        self.contexts = contexts
        self.preparations = preparations
        self.persistence = WritingPersistence(config, database, store, fault_hook)

    def import_unit(self, project_id: str, unit_id: str, raw: bytes) -> TextUnitSubmission:
        try:
            preparation = self.preparations.latest(project_id, unit_id)
        except NotFoundError as exc:
            raise ConflictError(
                "WRITING_PREPARATION_REQUIRED",
                f"Prepare unit {unit_id!r} before importing its output",
            ) from exc
        return self.import_for_preparation(project_id, preparation.id, raw)

    def import_for_preparation(
        self, project_id: str, preparation_id: str, raw: bytes
    ) -> TextUnitSubmission:
        with self.database.connection() as connection:
            project = ProjectRepository(connection).get(project_id)
            preparation = PreparationRepository(connection).get(preparation_id)
            if preparation.project_id != project.id:
                raise NotFoundError(
                    "WRITING_PREPARATION_NOT_FOUND", "Requested preparation was not found"
                )
        context = self.contexts.get_by_id(project_id, preparation.context_id)
        if not self.contexts.is_current(context):
            raise ConflictError(
                "WRITING_CONTEXT_STALE", "Cannot import into a stale WritingContext"
            )
        contract = self.contexts.resolve_contract(context)
        unit = contract.model.unit(preparation.unit_id)
        report = validate_submission(raw, contract.model, unit)
        report_bytes = validation_report_bytes(
            unit_id=unit.unit_id,
            character_count=report.character_count,
            disposition=report.disposition.value,
            findings=report.findings,
        )
        raw_sha = sha256_bytes(raw)
        report_sha = sha256_bytes(report_bytes)
        with self.database.connection() as connection:
            project = ProjectRepository(connection).get(project_id)
            preparations = PreparationRepository(connection)
            preparation = preparations.get(preparation_id)
            if preparation.context_id != context.id or preparation.unit_id != unit.unit_id:
                raise IntegrityError(
                    "WRITING_PREPARATION_BINDING_INVALID",
                    "Preparation does not match the bound context and unit",
                )
            preparation_run = StageRunRepository(connection).get(preparation.stage_run_id)
            if preparation_run.status is not RunStatus.DONE:
                raise IntegrityError(
                    "WRITING_PREPARATION_INCOMPLETE", "Unit preparation is not complete"
                )
            repository = SubmissionRepository(connection)
            existing = repository.by_preparation_hash(preparation.id, raw_sha)
            warning_count = sum(
                finding.severity is FindingSeverity.WARNING for finding in report.findings
            )
            input_hash = canonical_hash(
                {
                    "operation": "writing.unit.import",
                    "preparation_id": preparation.id,
                    "preparation_input_hash": preparation.input_hash,
                    "raw_sha256": raw_sha,
                }
            )
            if existing is not None:
                run = StageRunRepository(connection).get(existing.stage_run_id)
                if run.status is RunStatus.DONE:
                    return existing
                if not (
                    run.status is RunStatus.RUNNING
                    and run.input_hash == input_hash
                    and existing.disposition is SubmissionDisposition.PROCESSING
                    and existing.raw_sha256 == raw_sha
                    and existing.validation_report_sha256 == report_sha
                    and existing.character_count == report.character_count
                    and existing.warning_count == warning_count
                    and existing.raw_artifact_id is None
                    and existing.validation_report_artifact_id is None
                    and existing.accepted_artifact_id is None
                    and existing.citation_ledger_artifact_id is None
                    and repository.raw_version_used_by_other(existing)
                ):
                    raise IntegrityError(
                        "WRITING_RECOVERY_REQUIRED", "Submission import needs recovery"
                    )
                with self.database.transaction(connection):
                    current = repository.get(existing.id)
                    current_run = StageRunRepository(connection).get(existing.stage_run_id)
                    if (
                        current_run.status is not RunStatus.RUNNING
                        or current_run.input_hash != input_hash
                    ):
                        raise IntegrityError(
                            "WRITING_RECOVERY_REQUIRED", "Submission import needs recovery"
                        )
                    latest = repository.latest_for_project_unit(project.id, unit.unit_id)
                    raw_version = 1 if latest is None else latest.raw_version + 1
                    item = repository.reallocate_processing_raw_version(current, raw_version)
                running = current_run
            else:
                submission_id = new_id()
                with self.database.transaction(connection):
                    latest = repository.latest_for_project_unit(project.id, unit.unit_id)
                    raw_version = 1 if latest is None else latest.raw_version + 1
                    run = self.persistence.new_run(
                        project_id=project.id,
                        stage_id=TEXT_STAGE,
                        unit_id=f"import:{unit.unit_id}",
                        input_hash=input_hash,
                        version=raw_version,
                        supersedes_run_id=None if latest is None else latest.stage_run_id,
                    )
                    item = TextUnitSubmission(
                        id=submission_id,
                        project_id=project.id,
                        context_id=context.id,
                        preparation_id=preparation.id,
                        stage_run_id=run.id,
                        unit_id=unit.unit_id,
                        raw_version=raw_version,
                        accepted_version=None,
                        disposition=SubmissionDisposition.PROCESSING,
                        raw_sha256=raw_sha,
                        raw_artifact_id=None,
                        validation_report_sha256=report_sha,
                        validation_report_artifact_id=None,
                        accepted_artifact_id=None,
                        citation_ledger_sha256=None,
                        citation_ledger_artifact_id=None,
                        character_count=report.character_count,
                        warning_count=warning_count,
                        created_at=utc_now(),
                        accepted_at=None,
                    )
                    running = self.persistence.add_and_start(connection, run)
                    repository.add(item)
            accepted_version = (
                self._next_accepted_version(connection, project.id, unit.unit_id)
                if report.disposition is SubmissionDisposition.ACCEPTED
                else None
            )
            occurrences = (
                extract_citations(
                    project_id=project.id,
                    submission_id=item.id,
                    unit_id=unit.unit_id,
                    raw=raw,
                )
                if report.disposition is SubmissionDisposition.ACCEPTED
                else ()
            )
            ledger = citation_ledger_bytes(occurrences) if accepted_version is not None else None
            ledger_sha = None if ledger is None else sha256_bytes(ledger)
            raw_stored = self.store.write_bytes(
                project.artifact_root,
                f"text/raw/{unit.unit_id}/v{raw_version:04d}.txt",
                raw,
            )
            self.persistence.checkpoint("submission.raw")
            report_stored = self.store.write_bytes(
                project.artifact_root,
                f"text/validation/{unit.unit_id}/raw-v{raw_version:04d}.json",
                report_bytes,
            )
            self.persistence.checkpoint("submission.validation_report")
            accepted_stored = None
            ledger_stored = None
            if accepted_version is not None and ledger is not None:
                accepted_stored = self.store.write_bytes(
                    project.artifact_root,
                    f"text/accepted/{unit.unit_id}/v{accepted_version:04d}.txt",
                    raw,
                )
                self.persistence.checkpoint("submission.accepted")
                ledger_stored = self.store.write_bytes(
                    project.artifact_root,
                    f"text/references/{unit.unit_id}/accepted-v{accepted_version:04d}.json",
                    ledger,
                )
                self.persistence.checkpoint("submission.citation_ledger")
            with self.database.transaction(connection):
                raw_artifact = self.persistence.register_artifact(
                    connection,
                    project,
                    running,
                    "text_unit_raw",
                    raw_stored,
                    raw_version,
                )
                report_artifact = self.persistence.register_artifact(
                    connection,
                    project,
                    running,
                    "text_validation_report",
                    report_stored,
                    raw_version,
                )
                accepted_artifact_id = None
                ledger_artifact_id = None
                accepted_at = None
                if accepted_stored is not None and ledger_stored is not None:
                    accepted_artifact = self.persistence.register_artifact(
                        connection,
                        project,
                        running,
                        "text_unit_accepted",
                        accepted_stored,
                        accepted_version or 1,
                    )
                    ledger_artifact = self.persistence.register_artifact(
                        connection,
                        project,
                        running,
                        "citation_ledger",
                        ledger_stored,
                        accepted_version or 1,
                    )
                    accepted_artifact_id = accepted_artifact.id
                    ledger_artifact_id = ledger_artifact.id
                    accepted_at = utc_now()
                    CitationRepository(connection).add_all(occurrences)
                item = repository.materialize(
                    item,
                    disposition=report.disposition,
                    raw_artifact_id=raw_artifact.id,
                    report_artifact_id=report_artifact.id,
                    accepted_artifact_id=accepted_artifact_id,
                    citation_ledger_sha256=ledger_sha,
                    citation_ledger_artifact_id=ledger_artifact_id,
                    accepted_version=accepted_version,
                    accepted_at=accepted_at,
                )
                self.persistence.finish(connection, running.id)
            return item

    def confirm(self, project_id: str, unit_id: str, raw_version: int) -> TextUnitSubmission:
        context = self.contexts.latest(project_id)
        contract = self.contexts.resolve_contract(context)
        unit = contract.model.unit(unit_id)
        with self.database.connection() as connection:
            project = ProjectRepository(connection).get(project_id)
            repository = SubmissionRepository(connection)
            item = repository.by_raw_version(context.id, unit.unit_id, raw_version)
            if item is None:
                raise NotFoundError(
                    "WRITING_SUBMISSION_NOT_FOUND", "Submission raw version was not found"
                )
            if item.disposition is SubmissionDisposition.ACCEPTED:
                return item
            if item.disposition is not SubmissionDisposition.REVIEW_REQUIRED:
                raise ConflictError(
                    "WRITING_SUBMISSION_NOT_REVIEWABLE",
                    f"Submission is {item.disposition.value}, not review_required",
                )
            if item.raw_artifact_id is None:
                raise IntegrityError(
                    "WRITING_RAW_ARTIFACT_MISSING", "Submission raw artifact is not registered"
                )
            raw = self.persistence.read_artifact(
                connection, project, item.raw_artifact_id, expected_sha256=item.raw_sha256
            )
            report = validate_submission(raw, contract.model, unit)
            if any(finding.severity is FindingSeverity.ERROR for finding in report.findings):
                raise IntegrityError(
                    "WRITING_REVIEW_VALIDATION_CHANGED",
                    "Reviewed submission now contains a hard validation error",
                )
            input_hash = canonical_hash(
                {
                    "operation": "writing.unit.confirm",
                    "raw_sha256": item.raw_sha256,
                    "submission_id": item.id,
                }
            )
            runs = StageRunRepository(connection)
            unit_key = f"confirm:{item.id}"
            existing = runs.find_identity(project.id, TEXT_STAGE, unit_key, input_hash)
            if existing is not None and existing.status is RunStatus.DONE:
                return repository.get(item.id)
            run = self.persistence.new_run(
                project_id=project.id,
                stage_id=TEXT_STAGE,
                unit_id=unit_key,
                input_hash=input_hash,
                version=1,
            )
            accepted_version = self._next_accepted_version(connection, project.id, unit.unit_id)
            occurrences = extract_citations(
                project_id=project.id,
                submission_id=item.id,
                unit_id=unit.unit_id,
                raw=raw,
            )
            ledger = citation_ledger_bytes(occurrences)
            ledger_sha = sha256_bytes(ledger)
            with self.database.transaction(connection):
                running = self.persistence.add_and_start(connection, run)
            accepted_stored = self.store.write_bytes(
                project.artifact_root,
                f"text/accepted/{unit.unit_id}/v{accepted_version:04d}.txt",
                raw,
            )
            self.persistence.checkpoint("submission.confirmed_accepted")
            ledger_stored = self.store.write_bytes(
                project.artifact_root,
                f"text/references/{unit.unit_id}/accepted-v{accepted_version:04d}.json",
                ledger,
            )
            self.persistence.checkpoint("submission.confirmed_citation_ledger")
            with self.database.transaction(connection):
                accepted_artifact = self.persistence.register_artifact(
                    connection,
                    project,
                    running,
                    "text_unit_accepted",
                    accepted_stored,
                    accepted_version,
                )
                ledger_artifact = self.persistence.register_artifact(
                    connection,
                    project,
                    running,
                    "citation_ledger",
                    ledger_stored,
                    accepted_version,
                )
                CitationRepository(connection).add_all(occurrences)
                item = repository.accept_reviewed(
                    item,
                    accepted_artifact.id,
                    ledger_sha,
                    ledger_artifact.id,
                    accepted_version,
                    utc_now(),
                )
                self.persistence.finish(connection, running.id)
            return item

    def show(
        self,
        project_id: str,
        unit_id: str,
        *,
        version: int | None = None,
        raw: bool = False,
    ) -> tuple[TextUnitSubmission, bytes]:
        context = self.contexts.latest(project_id)
        with self.database.connection() as connection:
            project = ProjectRepository(connection).get(project_id)
            repository = SubmissionRepository(connection)
            item = (
                repository.latest(context.id, unit_id)
                if version is None
                else repository.by_raw_version(context.id, unit_id, version)
                if raw
                else repository.by_accepted_version(context.id, unit_id, version)
            )
            if item is None:
                raise NotFoundError(
                    "WRITING_SUBMISSION_NOT_FOUND", f"No submission exists for {unit_id!r}"
                )
            artifact_id = item.raw_artifact_id if raw else item.accepted_artifact_id
            if artifact_id is None:
                raise NotFoundError(
                    "WRITING_ACCEPTED_SUBMISSION_NOT_FOUND",
                    "Requested submission has no accepted artifact",
                )
            return item, self.persistence.read_artifact(
                connection, project, artifact_id, expected_sha256=item.raw_sha256
            )

    def validation_report(
        self, project_id: str, submission: TextUnitSubmission
    ) -> dict[str, object]:
        """Read the persisted report bound to one immutable submission after hash verification."""
        if submission.project_id != project_id:
            raise IntegrityError(
                "WRITING_SUBMISSION_PROJECT_MISMATCH",
                "Submission does not belong to the requested project",
            )
        with self.database.connection() as connection:
            project = ProjectRepository(connection).get(project_id)
            persisted = SubmissionRepository(connection).get(submission.id)
            if persisted.project_id != project.id:
                raise IntegrityError(
                    "WRITING_SUBMISSION_PROJECT_MISMATCH",
                    "Persisted submission does not belong to the requested project",
                )
            if persisted.validation_report_artifact_id is None:
                raise IntegrityError(
                    "WRITING_VALIDATION_REPORT_MISSING",
                    "Submission validation report is not registered",
                )
            raw = self.persistence.read_artifact(
                connection,
                project,
                persisted.validation_report_artifact_id,
                expected_sha256=persisted.validation_report_sha256,
            )
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise IntegrityError(
                "WRITING_VALIDATION_REPORT_INVALID",
                "Persisted validation report is not valid UTF-8 JSON",
            ) from exc
        if not isinstance(value, dict):
            raise IntegrityError(
                "WRITING_VALIDATION_REPORT_INVALID",
                "Persisted validation report must be a JSON object",
            )
        findings = value.get("findings")
        report_disposition = value.get("disposition")
        valid_dispositions = {persisted.disposition.value}
        if persisted.disposition is SubmissionDisposition.ACCEPTED:
            # Explicit confirmation promotes an immutable review report without rewriting it.
            valid_dispositions.add(SubmissionDisposition.REVIEW_REQUIRED.value)
        if (
            value.get("unit_id") != persisted.unit_id
            or value.get("character_count") != persisted.character_count
            or report_disposition not in valid_dispositions
            or not isinstance(findings, list)
        ):
            raise IntegrityError(
                "WRITING_VALIDATION_REPORT_DIVERGENT",
                "Persisted validation report differs from its submission",
            )
        error_count = 0
        warning_count = 0
        for finding in findings:
            if not isinstance(finding, dict) or not isinstance(finding.get("code"), str):
                raise IntegrityError(
                    "WRITING_VALIDATION_REPORT_INVALID",
                    "Persisted validation report has an invalid finding",
                )
            severity = finding.get("severity")
            if severity == FindingSeverity.ERROR.value:
                error_count += 1
            elif severity == FindingSeverity.WARNING.value:
                warning_count += 1
            else:
                raise IntegrityError(
                    "WRITING_VALIDATION_REPORT_INVALID",
                    "Persisted validation report has an unknown finding severity",
                )
        if warning_count != persisted.warning_count:
            raise IntegrityError(
                "WRITING_VALIDATION_REPORT_DIVERGENT",
                "Persisted validation report warning count differs from its submission",
            )
        return {
            **value,
            "error_count": error_count,
            "warning_count": warning_count,
        }

    @staticmethod
    def _next_accepted_version(
        connection: sqlite3.Connection, project_id: str, unit_id: str
    ) -> int:
        row = connection.execute(
            "SELECT max(accepted_version) FROM text_unit_submissions "
            "WHERE project_id = ? AND unit_id = ?",
            (project_id, unit_id),
        ).fetchone()
        assert row is not None
        return 1 if row[0] is None else int(row[0]) + 1

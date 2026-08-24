from __future__ import annotations

import sqlite3
from dataclasses import asdict
from typing import NoReturn

from ebook_pipeline.academic.service import AcademicService
from ebook_pipeline.config import AppConfig
from ebook_pipeline.core.errors import HeliosError, IntegrityError, NotFoundError
from ebook_pipeline.core.models import Project, RunStatus, StageRun, ValidationIssue
from ebook_pipeline.storage.artifacts import ArtifactStore
from ebook_pipeline.storage.database import Database
from ebook_pipeline.storage.repositories import (
    ErrorRepository,
    ProjectRepository,
    StageRunRepository,
)
from ebook_pipeline.writing.consolidation import TextConsolidator
from ebook_pipeline.writing.context import WritingContextManager
from ebook_pipeline.writing.models import (
    ProductionUnitStatus,
    SubmissionDisposition,
    TextConsolidation,
    TextProductionSet,
    TextUnitSubmission,
    WritingAcknowledgement,
    WritingContext,
    WritingUnitPreparation,
    WritingUnitRecoveryResult,
)
from ebook_pipeline.writing.persistence import FaultHook, WritingPersistence
from ebook_pipeline.writing.preparation import WritingPreparationManager
from ebook_pipeline.writing.production_set import ProductionSetResolver
from ebook_pipeline.writing.projections import (
    AcademicProjection,
    AcademicProjectionRepository,
    projection_manifest_bytes,
)
from ebook_pipeline.writing.recovery import WritingRecovery
from ebook_pipeline.writing.repositories import (
    AcknowledgementRepository,
    CitationRepository,
    ConsolidationRepository,
    PreparationRepository,
    SubmissionRepository,
    WritingArtifactRepository,
    WritingContextRepository,
)
from ebook_pipeline.writing.submissions import WritingSubmissionManager


class WritingService:
    def __init__(
        self,
        config: AppConfig,
        database: Database,
        store: ArtifactStore,
        academic: AcademicService | None = None,
        *,
        fault_hook: FaultHook | None = None,
    ) -> None:
        self.config = config
        self.database = database
        self.store = store
        self.academic = academic or AcademicService(config, database, store)
        self.contexts = WritingContextManager(config, database, store, self.academic, fault_hook)
        self.preparations = WritingPreparationManager(
            config, database, store, self.contexts, fault_hook
        )
        self.submissions = WritingSubmissionManager(
            config, database, store, self.contexts, self.preparations, fault_hook
        )
        self.consolidator = TextConsolidator(config, database, store, self.contexts, fault_hook)
        self.persistence = WritingPersistence(config, database, store, fault_hook)
        self.resolver = ProductionSetResolver()
        self.recovery = WritingRecovery(config, database, store, self.contexts)

    def create_context(self, project_id: str, **kwargs: object) -> WritingContext:
        return self.contexts.create(project_id, **kwargs)  # type: ignore[arg-type]

    def context(self, project_id: str, version: int | None = None) -> WritingContext:
        return self.contexts.get(project_id, version)

    def context_package(
        self, project_id: str, version: int | None = None
    ) -> tuple[WritingContext, bytes]:
        return self.contexts.package(project_id, version)

    def context_package_by_id(
        self, project_id: str, context_id: str
    ) -> tuple[WritingContext, bytes]:
        return self.contexts.package_by_id(project_id, context_id)

    def acknowledge_context(self, project_id: str, raw: bytes) -> WritingAcknowledgement:
        return self.contexts.acknowledge(project_id, raw)

    def acknowledge_context_by_id(
        self, project_id: str, context_id: str, raw: bytes
    ) -> WritingAcknowledgement:
        return self.contexts.acknowledge_for_context(project_id, context_id, raw)

    def confirm_context(
        self, project_id: str, raw_version: int | None = None
    ) -> WritingAcknowledgement:
        return self.contexts.confirm(project_id, raw_version)

    def confirm_context_by_id(
        self, project_id: str, context_id: str, raw_version: int | None = None
    ) -> WritingAcknowledgement:
        return self.contexts.confirm_for_context(project_id, context_id, raw_version)

    def prepare_unit(
        self, project_id: str, unit_id: str, *, reprocess: bool = False
    ) -> WritingUnitPreparation:
        return self.preparations.prepare(project_id, unit_id, reprocess=reprocess)

    def prepare_unit_for_context(
        self,
        project_id: str,
        context_id: str,
        unit_id: str,
        *,
        reprocess: bool = False,
    ) -> WritingUnitPreparation:
        return self.preparations.prepare_for_context(
            project_id, context_id, unit_id, reprocess=reprocess
        )

    def recover_unit_preparation(
        self, project_id: str, unit_id: str
    ) -> WritingUnitRecoveryResult:
        context = self.contexts.latest(project_id)
        with self.database.connection() as connection:
            project = ProjectRepository(connection).get(project_id)
            repository = PreparationRepository(connection)
            item = repository.latest(context.id, unit_id)
            if item is None:
                raise NotFoundError(
                    "WRITING_PREPARATION_NOT_FOUND",
                    f"No preparation exists for unit {unit_id!r} in the current context",
                )
            run = StageRunRepository(connection).get(item.stage_run_id)
            previous_version = item.version
            previous_status = run.status
            if run.status is RunStatus.DONE:
                return self._unit_recovery_result(
                    connection,
                    item,
                    run,
                    previous_version=previous_version,
                    previous_status=previous_status,
                    recovered=False,
                )
            if run.status is not RunStatus.RUNNING:
                self._raise_unit_recovery_error(connection, run.id)
            result = self.recovery.recover_run(connection, project, run)
            if result is None or result.status is not RunStatus.DONE:
                self._raise_unit_recovery_error(connection, run.id)
            recovered_item = repository.get(item.id)
            recovered_run = StageRunRepository(connection).get(run.id)
            return self._unit_recovery_result(
                connection,
                recovered_item,
                recovered_run,
                previous_version=previous_version,
                previous_status=previous_status,
                recovered=True,
            )

    @staticmethod
    def _raise_unit_recovery_error(
        connection: sqlite3.Connection, run_id: str
    ) -> NoReturn:
        error = ErrorRepository(connection).latest_for_run(run_id)
        if error is None:
            raise IntegrityError(
                "WRITING_RECOVERY_REQUIRED",
                "Unit preparation is incomplete and cannot be recovered automatically",
                recoverable=True,
            )
        raise IntegrityError(error.code, error.message, recoverable=error.recoverable)

    @staticmethod
    def _unit_recovery_result(
        connection: sqlite3.Connection,
        item: WritingUnitPreparation,
        run: StageRun,
        *,
        previous_version: int,
        previous_status: RunStatus,
        recovered: bool,
    ) -> WritingUnitRecoveryResult:
        current_run = StageRunRepository(connection).get(item.stage_run_id)
        if (
            current_run.id != run.id
            or current_run.status is not RunStatus.DONE
            or current_run.finished_at is None
            or item.request_artifact_id is None
            or item.manifest_artifact_id is None
        ):
            raise IntegrityError(
                "WRITING_RECOVERY_REQUIRED",
                "Recovered preparation lacks terminal StageRun or Artifact evidence",
            )
        artifacts = WritingArtifactRepository(connection)
        request = artifacts.get(item.request_artifact_id)
        manifest = artifacts.get(item.manifest_artifact_id)
        if (
            request.project_id != item.project_id
            or manifest.project_id != item.project_id
            or request.stage_run_id != current_run.id
            or manifest.stage_run_id != current_run.id
            or request.sha256 != item.request_sha256
            or manifest.sha256 != item.manifest_sha256
            or request.version != item.version
            or manifest.version != item.version
        ):
            raise IntegrityError(
                "WRITING_PREPARATION_ARTIFACT_BINDING_INVALID",
                "Recovered preparation Artifact bindings differ",
            )
        return WritingUnitRecoveryResult(
            project_id=item.project_id,
            context_id=item.context_id,
            unit_id=item.unit_id,
            preparation_id=item.id,
            previous_version=previous_version,
            version=item.version,
            stage_run_id=current_run.id,
            previous_stage_run_status=previous_status,
            stage_run_status=current_run.status,
            finished_at=current_run.finished_at,
            artifacts=(request, manifest),
            recovered=recovered,
        )

    def unit_request(self, project_id: str, unit_id: str) -> tuple[WritingUnitPreparation, bytes]:
        return self.preparations.request(project_id, unit_id)

    def unit_request_by_id(
        self, project_id: str, preparation_id: str
    ) -> tuple[WritingUnitPreparation, bytes]:
        return self.preparations.request_by_id(project_id, preparation_id)

    def import_unit(self, project_id: str, unit_id: str, raw: bytes) -> TextUnitSubmission:
        return self.submissions.import_unit(project_id, unit_id, raw)

    def import_unit_by_preparation_id(
        self, project_id: str, preparation_id: str, raw: bytes
    ) -> TextUnitSubmission:
        return self.submissions.import_for_preparation(project_id, preparation_id, raw)

    def confirm_unit(self, project_id: str, unit_id: str, raw_version: int) -> TextUnitSubmission:
        return self.submissions.confirm(project_id, unit_id, raw_version)

    def unit(
        self,
        project_id: str,
        unit_id: str,
        *,
        version: int | None = None,
        raw: bool = False,
    ) -> tuple[TextUnitSubmission, bytes]:
        return self.submissions.show(project_id, unit_id, version=version, raw=raw)

    def unit_validation_report(
        self, project_id: str, submission: TextUnitSubmission
    ) -> dict[str, object]:
        return self.submissions.validation_report(project_id, submission)

    def production_set(self, project_id: str) -> TextProductionSet:
        context = self.contexts.latest(project_id)
        contract = self.contexts.resolve_contract(context)
        current = self.contexts.is_current(context)
        with self.database.connection() as connection:
            ProjectRepository(connection).get(project_id)
            return self.resolver.resolve(
                connection,
                project_id=project_id,
                context_id=context.id,
                contract=contract,
                context_current=current,
            )

    def consolidate(self, project_id: str) -> TextConsolidation:
        return self.consolidator.consolidate(project_id)

    def consolidated(
        self, project_id: str, version: int | None = None
    ) -> tuple[TextConsolidation, bytes]:
        return self.consolidator.show(project_id, version)

    def status(self, project_id: str) -> dict[str, object]:
        try:
            context = self.contexts.latest(project_id)
        except NotFoundError:
            return {"project_id": project_id, "writing_context": {"status": "missing"}}
        production_set = self.production_set(project_id)
        counts = {status.value: 0 for status in ProductionUnitStatus}
        for selection in production_set.selections:
            counts[selection.status.value] += 1
        with self.database.connection() as connection:
            acknowledgement = AcknowledgementRepository(connection).confirmed(context.id)
            latest_submissions = {
                selection.unit_id: SubmissionRepository(connection).latest(
                    context.id, selection.unit_id
                )
                for selection in production_set.selections
            }
            consolidation = ConsolidationRepository(connection).latest(project_id)

        def latest_disposition(unit_id: str) -> str | None:
            latest = latest_submissions[unit_id]
            return None if latest is None else latest.disposition.value

        return {
            "project_id": project_id,
            "writing_context": {
                "confirmed": acknowledgement is not None,
                "current": production_set.context_current,
                "id": context.id,
                "version": context.version,
            },
            "production_set": {
                "complete": production_set.complete,
                "counts": counts,
                "current": production_set.current,
                "production_set_hash": production_set.production_set_hash,
                "units": [
                    {
                        "accepted_version": (
                            None
                            if selection.submission is None
                            else selection.submission.accepted_version
                        ),
                        "latest_disposition": latest_disposition(selection.unit_id),
                        "status": selection.status.value,
                        "submission_id": (
                            None if selection.submission is None else selection.submission.id
                        ),
                        "unit_id": selection.unit_id,
                    }
                    for selection in production_set.selections
                ],
            },
            "consolidation": (
                None
                if consolidation is None
                else {
                    "id": consolidation.id,
                    "production_set_hash": consolidation.production_set_hash,
                    "version": consolidation.version,
                }
            ),
        }

    def validate(self, project_id: str) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        with self.database.connection() as connection:
            project = ProjectRepository(connection).get(project_id)
            contexts = WritingContextRepository(connection).list_for_project(project_id)
            for context in contexts:
                try:
                    contract = self.contexts.resolve_contract(context)
                    self._check_artifact(
                        connection,
                        project,
                        context.package_artifact_id,
                        context.package_sha256,
                        "WRITING_CONTEXT_PACKAGE_INVALID",
                    )
                    self._check_artifact(
                        connection,
                        project,
                        context.manifest_artifact_id,
                        context.manifest_sha256,
                        "WRITING_CONTEXT_MANIFEST_INVALID",
                    )
                    if any(unit.academic_context is not None for unit in contract.model.units):
                        manifest_row = connection.execute(
                            "SELECT * FROM writing_context_projection_manifests "
                            "WHERE context_id = ?",
                            (context.id,),
                        ).fetchone()
                        if manifest_row is None:
                            raise IntegrityError(
                                "WRITING_PROJECTION_MANIFEST_MISSING",
                                "Automation context projection manifest is missing",
                            )
                        projections: list[AcademicProjection] = []
                        projection_repository = AcademicProjectionRepository(connection)
                        for unit in contract.model.ordered_units():
                            stored_projection = projection_repository.get(context.id, unit.unit_id)
                            if stored_projection is None:
                                raise IntegrityError(
                                    "WRITING_ACADEMIC_PROJECTION_MISSING",
                                    f"Projection for {unit.unit_id!r} is missing",
                                )
                            if (
                                stored_projection.project_id != project.id
                                or stored_projection.plan_document_id != context.plan_document_id
                                or stored_projection.plan_artifact_id != context.plan_artifact_id
                                or stored_projection.plan_sha256 != context.plan_sha256
                            ):
                                raise IntegrityError(
                                    "WRITING_ACADEMIC_PROJECTION_PROVENANCE_INVALID",
                                    f"Projection for {unit.unit_id!r} has divergent provenance",
                                )
                            projection_content = self._check_artifact(
                                connection,
                                project,
                                stored_projection.artifact_id,
                                stored_projection.academic_context_sha256,
                                "WRITING_ACADEMIC_PROJECTION_INVALID",
                            )
                            projections.append(
                                AcademicProjection(
                                    unit_id=stored_projection.unit_id,
                                    selector_json=stored_projection.selector_json,
                                    projection_version=stored_projection.projection_version,
                                    content=projection_content,
                                    sha256=stored_projection.academic_context_sha256,
                                )
                            )
                        manifest_content = self._check_artifact(
                            connection,
                            project,
                            str(manifest_row["manifest_artifact_id"]),
                            str(manifest_row["manifest_sha256"]),
                            "WRITING_PROJECTION_MANIFEST_INVALID",
                        )
                        expected_manifest = projection_manifest_bytes(
                            plan_document_id=context.plan_document_id,
                            plan_artifact_id=context.plan_artifact_id,
                            plan_sha256=context.plan_sha256,
                            projections=tuple(projections),
                        )
                        if manifest_content != expected_manifest:
                            raise IntegrityError(
                                "WRITING_PROJECTION_MANIFEST_DIVERGENT",
                                "Projection manifest differs from frozen projection rows",
                            )
                    for preparation in PreparationRepository(connection).list_for_context(
                        context.id
                    ):
                        contract.model.unit(preparation.unit_id)
                        self._check_artifact(
                            connection,
                            project,
                            preparation.request_artifact_id,
                            preparation.request_sha256,
                            "WRITING_PREPARATION_REQUEST_INVALID",
                        )
                        self._check_artifact(
                            connection,
                            project,
                            preparation.manifest_artifact_id,
                            preparation.manifest_sha256,
                            "WRITING_PREPARATION_MANIFEST_INVALID",
                        )
                    for submission in SubmissionRepository(connection).list_for_context(context.id):
                        raw = self._check_artifact(
                            connection,
                            project,
                            submission.raw_artifact_id,
                            submission.raw_sha256,
                            "WRITING_SUBMISSION_RAW_INVALID",
                        )
                        self._check_artifact(
                            connection,
                            project,
                            submission.validation_report_artifact_id,
                            submission.validation_report_sha256,
                            "WRITING_VALIDATION_REPORT_INVALID",
                        )
                        if submission.disposition is SubmissionDisposition.ACCEPTED:
                            accepted = self._check_artifact(
                                connection,
                                project,
                                submission.accepted_artifact_id,
                                submission.raw_sha256,
                                "WRITING_ACCEPTED_ARTIFACT_INVALID",
                            )
                            if raw != accepted:
                                raise IntegrityError(
                                    "WRITING_ACCEPTED_BYTES_CHANGED",
                                    "Accepted bytes differ from their raw submission",
                                )
                            self._check_artifact(
                                connection,
                                project,
                                submission.citation_ledger_artifact_id,
                                submission.citation_ledger_sha256 or "",
                                "CITATION_LEDGER_ARTIFACT_INVALID",
                            )
                            text = raw.decode("utf-8")
                            for occurrence in CitationRepository(connection).for_submission(
                                submission.id
                            ):
                                if (
                                    text[occurrence.start_offset : occurrence.end_offset]
                                    != occurrence.raw_citation_text
                                ):
                                    raise IntegrityError(
                                        "CITATION_OFFSET_INVALID",
                                        "Citation offsets do not reproduce the raw occurrence",
                                    )
                except HeliosError as exc:
                    issues.append(ValidationIssue(exc.code, exc.message))
            for consolidation in self._consolidations(connection, project_id):
                try:
                    self._check_artifact(
                        connection,
                        project,
                        consolidation.text_artifact_id,
                        consolidation.text_sha256,
                        "TEXT_CONSOLIDATION_ARTIFACT_INVALID",
                    )
                    self._check_artifact(
                        connection,
                        project,
                        consolidation.manifest_artifact_id,
                        consolidation.manifest_sha256,
                        "TEXT_CONSOLIDATION_MANIFEST_INVALID",
                    )
                except HeliosError as exc:
                    issues.append(ValidationIssue(exc.code, exc.message))
        return issues

    def _check_artifact(
        self,
        connection: sqlite3.Connection,
        project: Project,
        artifact_id: str | None,
        sha256: str,
        code: str,
    ) -> bytes:
        if artifact_id is None:
            raise IntegrityError(code, "Required writing artifact is not registered")
        try:
            return self.persistence.read_artifact(
                connection, project, artifact_id, expected_sha256=sha256
            )
        except HeliosError as exc:
            raise IntegrityError(code, exc.message) from exc

    @staticmethod
    def _consolidations(connection: sqlite3.Connection, project_id: str) -> list[TextConsolidation]:
        rows = connection.execute(
            "SELECT id FROM text_consolidations WHERE project_id = ? ORDER BY version",
            (project_id,),
        ).fetchall()
        repository = ConsolidationRepository(connection)
        return [repository.get(str(row["id"])) for row in rows]

    @staticmethod
    def context_payload(context: WritingContext) -> dict[str, object]:
        return asdict(context)

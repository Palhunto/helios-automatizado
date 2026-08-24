from __future__ import annotations

import json
import sqlite3

from ebook_pipeline.config import AppConfig
from ebook_pipeline.core.errors import HeliosError, IntegrityError
from ebook_pipeline.core.hashing import sha256_bytes
from ebook_pipeline.core.ids import utc_now
from ebook_pipeline.core.models import Project, RecoveryResult, RunStatus, StageRun, StoredFile
from ebook_pipeline.core.state_machine import StateMachine
from ebook_pipeline.storage.artifacts import ArtifactStore
from ebook_pipeline.storage.database import Database
from ebook_pipeline.storage.repositories import (
    ArtifactRepository,
    ErrorRepository,
    StageRunRepository,
)
from ebook_pipeline.writing.citations import citation_ledger_bytes, extract_citations
from ebook_pipeline.writing.consolidation import CONSOLIDATION_STAGE
from ebook_pipeline.writing.context import CONTEXT_STAGE, WritingContextManager
from ebook_pipeline.writing.models import (
    CitationOccurrence,
    FindingSeverity,
    SubmissionDisposition,
    WritingUnitPreparation,
)
from ebook_pipeline.writing.persistence import WritingPersistence
from ebook_pipeline.writing.preparation import (
    TEXT_STAGE,
    PreparationOutputs,
    build_preparation_outputs,
)
from ebook_pipeline.writing.projections import AcademicProjectionRepository
from ebook_pipeline.writing.repositories import (
    AcknowledgementRepository,
    CitationRepository,
    ConsolidationRepository,
    PreparationRepository,
    SubmissionRepository,
    WritingContextRepository,
)
from ebook_pipeline.writing.submissions import WritingSubmissionManager
from ebook_pipeline.writing.validators import validate_submission


class WritingRecovery:
    def __init__(
        self,
        config: AppConfig,
        database: Database,
        store: ArtifactStore,
        contexts: WritingContextManager,
    ) -> None:
        self.config = config
        self.database = database
        self.store = store
        self.contexts = contexts
        self.persistence = WritingPersistence(config, database, store)
        self.state_machine = StateMachine()

    def recover_run(
        self, connection: sqlite3.Connection, project: Project, run: StageRun
    ) -> RecoveryResult | None:
        try:
            return self._recover_run_unchecked(connection, project, run)
        except IntegrityError as exc:
            return self._blocked(connection, run, exc.code)

    def _recover_run_unchecked(
        self, connection: sqlite3.Connection, project: Project, run: StageRun
    ) -> RecoveryResult | None:
        if run.stage_id == CONTEXT_STAGE:
            if run.unit_id == "context:create":
                return self._recover_context(connection, project, run)
            if run.unit_id.startswith("context:ack:"):
                return self._recover_acknowledgement(connection, project, run)
            if run.unit_id.startswith("context:confirm:"):
                return self._recover_context_confirmation(connection, run)
        if run.stage_id == TEXT_STAGE:
            if run.unit_id.startswith("prepare:"):
                return self._recover_preparation(connection, project, run)
            if run.unit_id.startswith("import:"):
                return self._recover_submission(connection, project, run)
            if run.unit_id.startswith("confirm:"):
                return self._recover_submission_confirmation(connection, project, run)
        if run.stage_id == CONSOLIDATION_STAGE:
            return self._recover_consolidation(connection, project, run)
        return None

    def _recover_context(
        self, connection: sqlite3.Connection, project: Project, run: StageRun
    ) -> RecoveryResult:
        repository = WritingContextRepository(connection)
        context = repository.get_by_run(run.id)
        if context is None:
            return self._pending(connection, run, "WRITING_CONTEXT_METADATA_MISSING")
        package = self._inspect(
            project,
            f"text/context/v{context.version:04d}/package.txt",
            context.package_sha256,
        )
        manifest = self._inspect(
            project,
            f"text/context/v{context.version:04d}/manifest.json",
            context.manifest_sha256,
        )
        if package is None or manifest is None:
            return self._pending(connection, run, "WRITING_CONTEXT_OUTPUT_MISSING")
        prompt = self._inspect(
            project,
            f"prompts/frozen/{context.writing_prompt_id}/v{context.writing_prompt_version:04d}.txt",
            context.writing_prompt_sha256,
        )
        contract = self._inspect(
            project,
            f"writing-contracts/frozen/{context.contract_id}/v{context.contract_version:04d}.yaml",
            context.contract_sha256,
        )
        request_prompt = self._inspect(
            project,
            f"prompts/frozen/{context.request_prompt_id}/v{context.request_prompt_version:04d}.txt",
            context.request_prompt_sha256,
        )
        if prompt is None or contract is None or request_prompt is None:
            return self._pending(connection, run, "WRITING_CONTEXT_SNAPSHOT_MISSING")
        with self.database.transaction(connection):
            package_artifact = self.persistence.register_artifact(
                connection, project, run, "writing_context_package", package, context.version
            )
            manifest_artifact = self.persistence.register_artifact(
                connection, project, run, "writing_context_manifest", manifest, context.version
            )
            self.persistence.register_artifact(
                connection,
                project,
                run,
                "prompt_snapshot",
                prompt,
                context.writing_prompt_version,
            )
            self.persistence.register_artifact(
                connection,
                project,
                run,
                "writing_contract_snapshot",
                contract,
                context.contract_version,
            )
            self.persistence.register_artifact(
                connection,
                project,
                run,
                "writing_request_prompt_snapshot",
                request_prompt,
                context.request_prompt_version,
            )
            repository.set_artifacts(context, package_artifact.id, manifest_artifact.id)
            done = self._done(connection, run)
        return RecoveryResult(run.id, run.status, done.status, "WritingContext reconciled")

    def _recover_acknowledgement(
        self, connection: sqlite3.Connection, project: Project, run: StageRun
    ) -> RecoveryResult:
        repository = AcknowledgementRepository(connection)
        item = repository.get_by_run(run.id)
        if item is None:
            return self._pending(connection, run, "WRITING_ACKNOWLEDGEMENT_METADATA_MISSING")
        context = WritingContextRepository(connection).get(item.context_id)
        stored = self._inspect(
            project,
            f"text/context/v{context.version:04d}/acknowledgement/raw-v{item.raw_version:04d}.txt",
            item.raw_sha256,
        )
        if stored is None:
            return self._pending(connection, run, "WRITING_ACKNOWLEDGEMENT_RAW_MISSING")
        with self.database.transaction(connection):
            artifact = self.persistence.register_artifact(
                connection,
                project,
                run,
                "writing_context_acknowledgement_raw",
                stored,
                item.raw_version,
            )
            repository.set_artifact(item, artifact.id)
            done = self._done(connection, run)
        return RecoveryResult(run.id, run.status, done.status, "Acknowledgement reconciled")

    def _recover_context_confirmation(
        self, connection: sqlite3.Connection, run: StageRun
    ) -> RecoveryResult:
        item_id = run.unit_id.removeprefix("context:confirm:")
        repository = AcknowledgementRepository(connection)
        try:
            item = repository.get(item_id)
        except HeliosError:
            return self._pending(connection, run, "WRITING_ACKNOWLEDGEMENT_METADATA_MISSING")
        if item.raw_artifact_id is None:
            return self._pending(connection, run, "WRITING_ACKNOWLEDGEMENT_RAW_MISSING")
        with self.database.transaction(connection):
            repository.confirm(item, utc_now())
            done = self._done(connection, run)
        return RecoveryResult(run.id, run.status, done.status, "Context confirmation reconciled")

    def _recover_preparation(
        self, connection: sqlite3.Connection, project: Project, run: StageRun
    ) -> RecoveryResult:
        repository = PreparationRepository(connection)
        item = repository.get_by_run(run.id)
        if item is None:
            return self._pending(connection, run, "WRITING_PREPARATION_METADATA_MISSING")
        outputs = self._rebuild_preparation_outputs(connection, project, run, item)
        request_path = self._preparation_path(item.unit_id, item.version, "request.txt")
        manifest_path = self._preparation_path(item.unit_id, item.version, "manifest.json")
        request = self._inspect_if_matching(project, request_path, item.request_sha256)
        manifest = self._inspect_if_matching(project, manifest_path, item.manifest_sha256)
        if request is None or manifest is None:
            legacy_version = self._legacy_conflict_version(
                connection, project, run, item, outputs
            )
            if legacy_version is None:
                for path, expected_sha in (
                    (request_path, item.request_sha256),
                    (manifest_path, item.manifest_sha256),
                ):
                    if self._path_exists(project, path) and self._inspect_if_matching(
                        project, path, expected_sha
                    ) is None:
                        raise IntegrityError(
                            "WRITING_RECOVERY_HASH_MISMATCH",
                            f"Recovery artifact {path!r} has divergent bytes",
                        )
                return self._pending(connection, run, "WRITING_PREPARATION_OUTPUT_MISSING")
            if legacy_version != item.version:
                with self.database.transaction(connection):
                    item = repository.reallocate_running_version(item, legacy_version)
                request_path = self._preparation_path(
                    item.unit_id, item.version, "request.txt"
                )
                manifest_path = self._preparation_path(
                    item.unit_id, item.version, "manifest.json"
                )
            request = self.store.write_bytes(project.artifact_root, request_path, outputs.request)
            manifest = self.store.write_bytes(
                project.artifact_root, manifest_path, outputs.manifest
            )
        with self.database.transaction(connection):
            request_artifact = self.persistence.register_artifact(
                connection, project, run, "writing_unit_request", request, item.version
            )
            manifest_artifact = self.persistence.register_artifact(
                connection, project, run, "writing_continuity_manifest", manifest, item.version
            )
            repository.set_artifacts(item, request_artifact.id, manifest_artifact.id)
            done = self._done(connection, run)
        return RecoveryResult(run.id, run.status, done.status, "Preparation reconciled")

    def _rebuild_preparation_outputs(
        self,
        connection: sqlite3.Connection,
        project: Project,
        run: StageRun,
        item: WritingUnitPreparation,
    ) -> PreparationOutputs:
        preparation = PreparationRepository(connection).get_by_run(run.id)
        if preparation is None or preparation.id != item.id:
            raise IntegrityError(
                "WRITING_PREPARATION_BINDING_INVALID",
                "Preparation no longer matches its StageRun",
            )
        if (
            preparation.project_id != project.id
            or preparation.stage_run_id != run.id
            or preparation.input_hash != run.input_hash
        ):
            raise IntegrityError(
                "WRITING_PREPARATION_BINDING_INVALID",
                "Preparation and StageRun provenance differ",
            )
        context = WritingContextRepository(connection).get(preparation.context_id)
        if context.project_id != project.id:
            raise IntegrityError(
                "WRITING_PREPARATION_BINDING_INVALID",
                "Preparation context belongs to a different project",
            )
        contract = self.contexts.resolve_contract(context)
        unit = contract.model.unit(preparation.unit_id)
        persisted = PreparationRepository(connection).dependencies(preparation.id)
        by_unit = {dependency.dependency_unit_id: dependency for dependency in persisted}
        if (
            set(by_unit) != set(unit.continuity_from)
            or len(by_unit) != len(persisted)
            or any(
                dependency.preparation_id != preparation.id
                or dependency.project_id != project.id
                or dependency.context_id != context.id
                for dependency in persisted
            )
        ):
            raise IntegrityError(
                "WRITING_CONTINUITY_PROVENANCE_INVALID",
                "Preparation dependencies differ from the frozen unit contract",
            )
        dependencies = tuple(by_unit[unit_id] for unit_id in unit.continuity_from)
        continuity_contents = tuple(
            (
                dependency,
                self.persistence.read_artifact(
                    connection,
                    project,
                    dependency.artifact_id,
                    expected_sha256=dependency.sha256,
                ),
            )
            for dependency in dependencies
        )
        projection = AcademicProjectionRepository(connection).get(context.id, unit.unit_id)
        academic_context = None
        if projection is not None:
            if (
                projection.project_id != project.id
                or projection.context_id != context.id
                or projection.unit_id != unit.unit_id
            ):
                raise IntegrityError(
                    "WRITING_ACADEMIC_PROJECTION_PROVENANCE_INVALID",
                    "Preparation projection binding differs",
                )
            academic_context = self.persistence.read_artifact(
                connection,
                project,
                projection.artifact_id,
                expected_sha256=projection.academic_context_sha256,
            )
        request_prompt = self.contexts.prompts.resolve(
            context.request_prompt_id, context.request_prompt_version
        )
        if request_prompt.sha256 != context.request_prompt_sha256:
            raise IntegrityError(
                "WRITING_REQUEST_PROMPT_BINDING_INVALID",
                "Context unit request prompt binding differs",
            )
        outputs = build_preparation_outputs(
            project_id=project.id,
            context=context,
            contract=contract,
            unit=unit,
            request_prompt=request_prompt,
            dependencies=dependencies,
            continuity_contents=continuity_contents,
            projection=projection,
            academic_context=academic_context,
        )
        if (
            outputs.input_hash != preparation.input_hash
            or outputs.request_sha256 != preparation.request_sha256
            or outputs.manifest_sha256 != preparation.manifest_sha256
        ):
            raise IntegrityError(
                "WRITING_PREPARATION_REBUILD_DIVERGENT",
                "Persisted preparation bindings do not reproduce its frozen hashes",
            )
        return outputs

    def _legacy_conflict_version(
        self,
        connection: sqlite3.Connection,
        project: Project,
        run: StageRun,
        item: WritingUnitPreparation,
        outputs: PreparationOutputs,
    ) -> int | None:
        repository = PreparationRepository(connection)
        preparation = repository.get_by_run(run.id)
        if preparation is None or preparation.id != item.id:
            return None
        if (
            run.status is not RunStatus.RUNNING
            or run.finished_at is not None
            or run.supersedes_run_id is not None
            or preparation.request_artifact_id is not None
            or preparation.manifest_artifact_id is not None
            or repository.has_later_preparation(preparation)
        ):
            return None
        if connection.execute(
            "SELECT 1 FROM artifacts WHERE stage_run_id = ? LIMIT 1", (run.id,)
        ).fetchone() is not None:
            return None
        source_version = run.version
        sources = [
            candidate
            for candidate in repository.at_project_unit_version(
                project.id, preparation.unit_id, source_version
            )
            if candidate.id != preparation.id
        ]
        if len(sources) != 1:
            return None
        source = sources[0]
        source_run = StageRunRepository(connection).get(source.stage_run_id)
        if (
            source.context_id == preparation.context_id
            or source.project_id != preparation.project_id
            or source.created_at >= preparation.created_at
            or source_run.status is not RunStatus.DONE
            or source_run.finished_at is None
            or source.request_artifact_id is None
            or source.manifest_artifact_id is None
        ):
            return None
        artifacts = ArtifactRepository(connection)
        source_request_path = self._preparation_path(
            source.unit_id, source.version, "request.txt"
        )
        source_manifest_path = self._preparation_path(
            source.unit_id, source.version, "manifest.json"
        )
        source_request = artifacts.get_by_path(project.id, source_request_path)
        source_manifest = artifacts.get_by_path(project.id, source_manifest_path)
        if (
            source_request is None
            or source_manifest is None
            or source_request.id != source.request_artifact_id
            or source_manifest.id != source.manifest_artifact_id
            or source_request.stage_run_id != source.stage_run_id
            or source_manifest.stage_run_id != source.stage_run_id
            or source_request.artifact_type != "writing_unit_request"
            or source_manifest.artifact_type != "writing_continuity_manifest"
            or source_request.version != source.version
            or source_manifest.version != source.version
            or source_request.sha256 != source.request_sha256
            or source_manifest.sha256 != source.manifest_sha256
        ):
            return None
        occupied_request = self._inspect_if_matching(
            project, source_request_path, source.request_sha256
        )
        occupied_manifest = self._inspect_if_matching(
            project, source_manifest_path, source.manifest_sha256
        )
        if (
            occupied_request is None
            or occupied_manifest is None
            or occupied_request.byte_size != source_request.byte_size
            or occupied_manifest.byte_size != source_manifest.byte_size
        ):
            return None
        if preparation.version == source_version:
            if (
                source.request_sha256 == outputs.request_sha256
                and source.manifest_sha256 == outputs.manifest_sha256
            ):
                return None
            latest = repository.latest_for_project_unit(project.id, preparation.unit_id)
            if latest is None or latest.id != preparation.id:
                return None
            target_version = max(
                candidate.version
                for candidate in repository.at_project_unit_version(
                    project.id, preparation.unit_id, source_version
                )
            ) + 1
        elif preparation.version > source_version:
            if repository.at_project_unit_version(
                project.id, preparation.unit_id, preparation.version
            ) != [preparation]:
                return None
            target_version = preparation.version
        else:
            return None
        target_request_path = self._preparation_path(
            preparation.unit_id, target_version, "request.txt"
        )
        target_manifest_path = self._preparation_path(
            preparation.unit_id, target_version, "manifest.json"
        )
        for path, expected_sha in (
            (target_request_path, outputs.request_sha256),
            (target_manifest_path, outputs.manifest_sha256),
        ):
            registered = artifacts.get_by_path(project.id, path)
            stored = self._inspect_if_matching(project, path, expected_sha)
            if registered is not None or (
                self._path_exists(project, path) and stored is None
            ):
                return None
        return target_version

    def _inspect_if_matching(
        self, project: Project, relative_path: str, expected_sha256: str
    ) -> StoredFile | None:
        try:
            stored = self.store.inspect(project.artifact_root, relative_path)
        except HeliosError:
            return None
        return stored if stored.sha256 == expected_sha256 else None

    def _path_exists(self, project: Project, relative_path: str) -> bool:
        try:
            self.store.inspect(project.artifact_root, relative_path)
        except HeliosError:
            return False
        return True

    @staticmethod
    def _preparation_path(unit_id: str, version: int, filename: str) -> str:
        return f"text/preparations/{unit_id}/v{version:04d}/{filename}"

    def _recover_submission(
        self, connection: sqlite3.Connection, project: Project, run: StageRun
    ) -> RecoveryResult:
        repository = SubmissionRepository(connection)
        item = repository.get_by_run(run.id)
        if item is None:
            return self._pending(connection, run, "WRITING_SUBMISSION_METADATA_MISSING")
        context = WritingContextRepository(connection).get(item.context_id)
        contract = self.contexts.resolve_contract(context)
        unit = contract.model.unit(item.unit_id)
        raw_stored = self._inspect(
            project, f"text/raw/{item.unit_id}/v{item.raw_version:04d}.txt", item.raw_sha256
        )
        report_stored = self._inspect(
            project,
            f"text/validation/{item.unit_id}/raw-v{item.raw_version:04d}.json",
            item.validation_report_sha256,
        )
        if raw_stored is None or report_stored is None:
            return self._pending(connection, run, "WRITING_SUBMISSION_OUTPUT_MISSING")
        raw = self.store.resolve(project.artifact_root, raw_stored.relative_path).read_bytes()
        validation = validate_submission(raw, contract.model, unit)
        try:
            report_value = json.loads(
                self.store.resolve(project.artifact_root, report_stored.relative_path).read_text(
                    encoding="utf-8"
                )
            )
            reported_disposition = SubmissionDisposition(report_value["disposition"])
        except (OSError, UnicodeError, ValueError, KeyError, json.JSONDecodeError):
            return self._blocked(connection, run, "WRITING_VALIDATION_REPORT_CORRUPT")
        if reported_disposition is not validation.disposition:
            return self._blocked(connection, run, "WRITING_VALIDATION_REPORT_DIVERGENT")

        accepted_version = None
        accepted_stored = None
        ledger_stored = None
        ledger_sha = None
        occurrences: tuple[CitationOccurrence, ...] = ()
        if validation.disposition is SubmissionDisposition.ACCEPTED:
            accepted_version = WritingSubmissionManager._next_accepted_version(
                connection, project.id, item.unit_id
            )
            occurrences = extract_citations(
                project_id=project.id,
                submission_id=item.id,
                unit_id=item.unit_id,
                raw=raw,
            )
            ledger = citation_ledger_bytes(occurrences)
            ledger_sha = sha256_bytes(ledger)
            accepted_stored = self.store.write_bytes(
                project.artifact_root,
                f"text/accepted/{item.unit_id}/v{accepted_version:04d}.txt",
                raw,
            )
            ledger_stored = self.store.write_bytes(
                project.artifact_root,
                f"text/references/{item.unit_id}/accepted-v{accepted_version:04d}.json",
                ledger,
            )
        with self.database.transaction(connection):
            raw_artifact = self.persistence.register_artifact(
                connection, project, run, "text_unit_raw", raw_stored, item.raw_version
            )
            report_artifact = self.persistence.register_artifact(
                connection,
                project,
                run,
                "text_validation_report",
                report_stored,
                item.raw_version,
            )
            accepted_artifact_id = None
            ledger_artifact_id = None
            accepted_at = None
            if accepted_stored is not None and ledger_stored is not None:
                accepted_artifact = self.persistence.register_artifact(
                    connection,
                    project,
                    run,
                    "text_unit_accepted",
                    accepted_stored,
                    accepted_version or 1,
                )
                ledger_artifact = self.persistence.register_artifact(
                    connection,
                    project,
                    run,
                    "citation_ledger",
                    ledger_stored,
                    accepted_version or 1,
                )
                accepted_artifact_id = accepted_artifact.id
                ledger_artifact_id = ledger_artifact.id
                accepted_at = utc_now()
                CitationRepository(connection).add_all(occurrences)
            repository.materialize(
                item,
                disposition=validation.disposition,
                raw_artifact_id=raw_artifact.id,
                report_artifact_id=report_artifact.id,
                accepted_artifact_id=accepted_artifact_id,
                citation_ledger_sha256=ledger_sha,
                citation_ledger_artifact_id=ledger_artifact_id,
                accepted_version=accepted_version,
                accepted_at=accepted_at,
            )
            done = self._done(connection, run)
        return RecoveryResult(run.id, run.status, done.status, "Submission reconciled")

    def _recover_submission_confirmation(
        self, connection: sqlite3.Connection, project: Project, run: StageRun
    ) -> RecoveryResult:
        submission_id = run.unit_id.removeprefix("confirm:")
        repository = SubmissionRepository(connection)
        try:
            item = repository.get(submission_id)
        except HeliosError:
            return self._pending(connection, run, "WRITING_SUBMISSION_METADATA_MISSING")
        if item.disposition is SubmissionDisposition.ACCEPTED:
            with self.database.transaction(connection):
                done = self._done(connection, run)
            return RecoveryResult(run.id, run.status, done.status, "Confirmation reconciled")
        if (
            item.disposition is not SubmissionDisposition.REVIEW_REQUIRED
            or item.raw_artifact_id is None
        ):
            return self._blocked(connection, run, "WRITING_CONFIRMATION_PROVENANCE_INVALID")
        context = WritingContextRepository(connection).get(item.context_id)
        contract = self.contexts.resolve_contract(context)
        raw = self.persistence.read_artifact(
            connection, project, item.raw_artifact_id, expected_sha256=item.raw_sha256
        )
        validation = validate_submission(raw, contract.model, contract.model.unit(item.unit_id))
        if any(finding.severity is FindingSeverity.ERROR for finding in validation.findings):
            return self._blocked(connection, run, "WRITING_CONFIRMATION_VALIDATION_INVALID")
        accepted_version = WritingSubmissionManager._next_accepted_version(
            connection, project.id, item.unit_id
        )
        occurrences = extract_citations(
            project_id=project.id,
            submission_id=item.id,
            unit_id=item.unit_id,
            raw=raw,
        )
        ledger = citation_ledger_bytes(occurrences)
        ledger_sha = sha256_bytes(ledger)
        accepted_stored = self.store.write_bytes(
            project.artifact_root,
            f"text/accepted/{item.unit_id}/v{accepted_version:04d}.txt",
            raw,
        )
        ledger_stored = self.store.write_bytes(
            project.artifact_root,
            f"text/references/{item.unit_id}/accepted-v{accepted_version:04d}.json",
            ledger,
        )
        with self.database.transaction(connection):
            accepted_artifact = self.persistence.register_artifact(
                connection,
                project,
                run,
                "text_unit_accepted",
                accepted_stored,
                accepted_version,
            )
            ledger_artifact = self.persistence.register_artifact(
                connection,
                project,
                run,
                "citation_ledger",
                ledger_stored,
                accepted_version,
            )
            CitationRepository(connection).add_all(occurrences)
            repository.accept_reviewed(
                item,
                accepted_artifact.id,
                ledger_sha,
                ledger_artifact.id,
                accepted_version,
                utc_now(),
            )
            done = self._done(connection, run)
        return RecoveryResult(run.id, run.status, done.status, "Confirmation reconciled")

    def _recover_consolidation(
        self, connection: sqlite3.Connection, project: Project, run: StageRun
    ) -> RecoveryResult:
        repository = ConsolidationRepository(connection)
        item = repository.get_by_run(run.id)
        if item is None:
            return self._pending(connection, run, "TEXT_CONSOLIDATION_METADATA_MISSING")
        text = self._inspect(
            project, f"text/consolidated/v{item.version:04d}.txt", item.text_sha256
        )
        manifest = self._inspect(
            project,
            f"text/consolidated/v{item.version:04d}.manifest.json",
            item.manifest_sha256,
        )
        if text is None or manifest is None:
            return self._pending(connection, run, "TEXT_CONSOLIDATION_OUTPUT_MISSING")
        try:
            manifest_value = json.loads(
                self.store.resolve(project.artifact_root, manifest.relative_path).read_text(
                    encoding="utf-8"
                )
            )
            if manifest_value["production_set_hash"] != item.production_set_hash:
                raise ValueError("production set hash differs")
            manifest_units = manifest_value["units"]
            if not isinstance(manifest_units, list):
                raise ValueError("units must be a list")
        except (OSError, UnicodeError, ValueError, KeyError, json.JSONDecodeError):
            return self._blocked(connection, run, "TEXT_CONSOLIDATION_MANIFEST_CORRUPT")
        with self.database.transaction(connection):
            text_artifact = self.persistence.register_artifact(
                connection, project, run, "text_consolidated", text, item.version
            )
            manifest_artifact = self.persistence.register_artifact(
                connection,
                project,
                run,
                "text_consolidation_manifest",
                manifest,
                item.version,
            )
            repository.set_artifacts(item, text_artifact.id, manifest_artifact.id)
            existing_units = {str(row["unit_id"]) for row in repository.members(item.id)}
            submissions = SubmissionRepository(connection)
            for order, member in enumerate(manifest_units, start=1):
                unit_id = str(member["unit_id"])
                if unit_id in existing_units:
                    continue
                submission = submissions.get(str(member["submission_id"]))
                if (
                    submission.accepted_version != int(member["accepted_version"])
                    or submission.accepted_artifact_id != str(member["artifact_id"])
                    or submission.raw_sha256 != str(member["sha256"])
                ):
                    raise IntegrityError(
                        "TEXT_CONSOLIDATION_PROVENANCE_INVALID",
                        "Consolidation member differs from accepted submission provenance",
                    )
                repository.add_member(
                    consolidation=item,
                    unit_id=unit_id,
                    unit_order=order,
                    submission=submission,
                    artifact_id=str(member["artifact_id"]),
                    sha256=str(member["sha256"]),
                )
            done = self._done(connection, run)
        return RecoveryResult(run.id, run.status, done.status, "Consolidation reconciled")

    def _inspect(
        self, project: Project, relative_path: str, expected_sha256: str
    ) -> StoredFile | None:
        try:
            stored = self.store.inspect(project.artifact_root, relative_path)
        except HeliosError:
            return None
        if stored.sha256 != expected_sha256:
            raise IntegrityError(
                "WRITING_RECOVERY_HASH_MISMATCH",
                f"Recovery artifact {relative_path!r} has divergent bytes",
            )
        return stored

    def _done(self, connection: sqlite3.Connection, run: StageRun) -> StageRun:
        current = StageRunRepository(connection).get(run.id)
        done = self.state_machine.transition(current, RunStatus.DONE, recovery=True)
        StageRunRepository(connection).update(done, expected_status=current.status)
        return done

    def _pending(self, connection: sqlite3.Connection, run: StageRun, code: str) -> RecoveryResult:
        message = code.replace("_", " ").title()
        with self.database.transaction(connection):
            ErrorRepository(connection).add(
                project_id=run.project_id,
                stage_run_id=run.id,
                code=code,
                message=message,
                recoverable=True,
            )
            retry = self.state_machine.transition(run, RunStatus.PENDING_RETRY, recovery=True)
            StageRunRepository(connection).update(retry, expected_status=run.status)
        return RecoveryResult(run.id, run.status, retry.status, message)

    def _blocked(self, connection: sqlite3.Connection, run: StageRun, code: str) -> RecoveryResult:
        message = code.replace("_", " ").title()
        with self.database.transaction(connection):
            ErrorRepository(connection).add(
                project_id=run.project_id,
                stage_run_id=run.id,
                code=code,
                message=message,
                recoverable=False,
            )
            failed = self.state_machine.transition(run, RunStatus.FAILED)
            StageRunRepository(connection).update(failed, expected_status=run.status)
            blocked = self.state_machine.transition(failed, RunStatus.BLOCKED)
            StageRunRepository(connection).update(blocked, expected_status=failed.status)
        return RecoveryResult(run.id, run.status, blocked.status, message)

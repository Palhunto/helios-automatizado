from __future__ import annotations

import sqlite3
from dataclasses import asdict

from ebook_pipeline.academic.models import CurrentWritingInputs
from ebook_pipeline.academic.service import AcademicService
from ebook_pipeline.config import AppConfig, load_project_config
from ebook_pipeline.core.errors import (
    ConflictError,
    IntegrityError,
    NotFoundError,
    WritingValidationError,
)
from ebook_pipeline.core.hashing import canonical_hash, sha256_bytes
from ebook_pipeline.core.ids import new_id, utc_now
from ebook_pipeline.core.models import Project, RunStatus, StageRun, StoredFile
from ebook_pipeline.prompts import PromptRegistry
from ebook_pipeline.storage.artifacts import ArtifactStore
from ebook_pipeline.storage.database import Database
from ebook_pipeline.storage.repositories import (
    ArtifactRepository,
    ProjectRepository,
    StageRunRepository,
)
from ebook_pipeline.writing.contracts import ResolvedWritingContract, WritingContractRegistry
from ebook_pipeline.writing.models import WritingAcknowledgement, WritingContext
from ebook_pipeline.writing.persistence import FaultHook, WritingPersistence
from ebook_pipeline.writing.projections import (
    AcademicProjection,
    AcademicProjectionRepository,
    projection_manifest_bytes,
    resolve_academic_projections,
)
from ebook_pipeline.writing.rendering import context_manifest, render_context_package
from ebook_pipeline.writing.repositories import AcknowledgementRepository, WritingContextRepository

CONTEXT_STAGE = "writing_context_load"
DEFAULT_CONTRACT_ID = "omega_writing_production"
DEFAULT_CONTRACT_VERSION = 1
DEFAULT_REQUEST_PROMPT_ID = "helios_writing_unit_request"
DEFAULT_REQUEST_PROMPT_VERSION = 1


class WritingContextManager:
    def __init__(
        self,
        config: AppConfig,
        database: Database,
        store: ArtifactStore,
        academic: AcademicService,
        fault_hook: FaultHook | None = None,
    ) -> None:
        self.config = config
        self.database = database
        self.store = store
        self.academic = academic
        self.prompts = PromptRegistry(config.prompt_registry)
        self.contracts = WritingContractRegistry(config.writing_contract_registry)
        self.persistence = WritingPersistence(config, database, store, fault_hook)

    def create(
        self,
        project_id: str,
        *,
        contract_id: str = DEFAULT_CONTRACT_ID,
        contract_version: int = DEFAULT_CONTRACT_VERSION,
        request_prompt_id: str = DEFAULT_REQUEST_PROMPT_ID,
        request_prompt_version: int = DEFAULT_REQUEST_PROMPT_VERSION,
    ) -> WritingContext:
        inputs = self.academic.get_current_writing_inputs(project_id)
        if inputs is None:
            raise ConflictError(
                "WRITING_ACADEMIC_INPUTS_NOT_CURRENT",
                "Current accepted Answers and a compatible Academic Plan are required",
            )
        if inputs.plan.document.upstream_document_id != inputs.answers.document.id:
            raise IntegrityError(
                "WRITING_ACADEMIC_PROVENANCE_INVALID",
                "Academic Plan does not derive from the selected Answers",
            )
        with self.database.connection() as connection:
            project = ProjectRepository(connection).get(project_id)
            loaded = load_project_config(
                self.store.resolve(project.artifact_root, project.config_path)
            )
            writing_reference = loaded.model.prompts.writing
            prompt = self.prompts.resolve(writing_reference.id, writing_reference.version)
            contract = self.contracts.resolve(contract_id, contract_version)
            request_prompt = self.prompts.resolve(request_prompt_id, request_prompt_version)
            projections: tuple[AcademicProjection, ...] = ()
            projection_manifest = None
            if any(unit.academic_context is not None for unit in contract.model.units):
                projections = resolve_academic_projections(inputs.plan.content, contract.model)
                projection_manifest = projection_manifest_bytes(
                    plan_document_id=inputs.plan.document.id,
                    plan_artifact_id=inputs.plan.artifact.id,
                    plan_sha256=inputs.plan.artifact.sha256,
                    projections=projections,
                )
            package = render_context_package(prompt, inputs)
            manifest = context_manifest(
                project_id=project.id,
                inputs=inputs,
                prompt=prompt,
                contract=contract,
                request_prompt=request_prompt,
                package=package,
            )
            package_sha = sha256_bytes(package)
            manifest_sha = sha256_bytes(manifest)
            input_hash = canonical_hash(
                {
                    "answers": (inputs.answers.document.id, inputs.answers.artifact.sha256),
                    "contract": (contract.id, contract.version, contract.sha256),
                    "operation": "writing.context.create",
                    "package_sha256": package_sha,
                    "projection_manifest_sha256": (
                        None
                        if projection_manifest is None
                        else sha256_bytes(projection_manifest)
                    ),
                    "plan": (inputs.plan.document.id, inputs.plan.artifact.sha256),
                    "request_prompt": (
                        request_prompt.id,
                        request_prompt.version,
                        request_prompt.sha256,
                    ),
                    "writing_prompt": (prompt.id, prompt.version, prompt.sha256),
                }
            )
            repository = WritingContextRepository(connection)
            existing = repository.by_input(project.id, input_hash)
            if existing is not None:
                run = StageRunRepository(connection).get(existing.stage_run_id)
                if run.status is not RunStatus.DONE:
                    raise IntegrityError(
                        "WRITING_RECOVERY_REQUIRED",
                        "Writing context creation was interrupted; recover first",
                        recoverable=True,
                    )
                return existing
            latest = repository.latest(project.id)
            version = 1 if latest is None else latest.version + 1
            run = self.persistence.new_run(
                project_id=project.id,
                stage_id=CONTEXT_STAGE,
                unit_id="context:create",
                input_hash=input_hash,
                version=version,
                supersedes_run_id=None if latest is None else latest.stage_run_id,
            )
            context = WritingContext(
                id=new_id(),
                project_id=project.id,
                stage_run_id=run.id,
                version=version,
                input_hash=input_hash,
                answers_document_id=inputs.answers.document.id,
                answers_artifact_id=inputs.answers.artifact.id,
                answers_sha256=inputs.answers.artifact.sha256,
                plan_document_id=inputs.plan.document.id,
                plan_artifact_id=inputs.plan.artifact.id,
                plan_sha256=inputs.plan.artifact.sha256,
                writing_prompt_id=prompt.id,
                writing_prompt_version=prompt.version,
                writing_prompt_sha256=prompt.sha256,
                contract_id=contract.id,
                contract_version=contract.version,
                contract_sha256=contract.sha256,
                request_prompt_id=request_prompt.id,
                request_prompt_version=request_prompt.version,
                request_prompt_sha256=request_prompt.sha256,
                package_sha256=package_sha,
                manifest_sha256=manifest_sha,
                package_artifact_id=None,
                manifest_artifact_id=None,
                created_at=utc_now(),
            )
            with self.database.transaction(connection):
                self._assert_inputs_still_current(inputs)
                running = self.persistence.add_and_start(connection, run)
                repository.add(context)

            package_stored = self.store.write_bytes(
                project.artifact_root, f"text/context/v{version:04d}/package.txt", package
            )
            self.persistence.checkpoint("context.package")
            manifest_stored = self.store.write_bytes(
                project.artifact_root, f"text/context/v{version:04d}/manifest.json", manifest
            )
            prompt_stored = self.store.write_bytes(
                project.artifact_root,
                f"prompts/frozen/{prompt.id}/v{prompt.version:04d}.txt",
                prompt.content,
            )
            contract_stored = self.store.write_bytes(
                project.artifact_root,
                f"writing-contracts/frozen/{contract.id}/v{contract.version:04d}.yaml",
                contract.content,
            )
            request_stored = self.store.write_bytes(
                project.artifact_root,
                f"prompts/frozen/{request_prompt.id}/v{request_prompt.version:04d}.txt",
                request_prompt.content,
            )
            projection_manifest_stored = None
            projection_stored: list[tuple[AcademicProjection, StoredFile]] = []
            if projection_manifest is not None:
                projection_manifest_stored = self.store.write_bytes(
                    project.artifact_root,
                    f"text/context/v{version:04d}/academic-projections/manifest.json",
                    projection_manifest,
                )
                for projection in projections:
                    stored = self.store.write_bytes(
                        project.artifact_root,
                        f"text/context/v{version:04d}/academic-projections/"
                        f"{projection.unit_id}.txt",
                        projection.content,
                    )
                    projection_stored.append((projection, stored))
            self.persistence.checkpoint("context.artifacts")
            with self.database.transaction(connection):
                package_artifact = self.persistence.register_artifact(
                    connection, project, running, "writing_context_package", package_stored, version
                )
                manifest_artifact = self.persistence.register_artifact(
                    connection,
                    project,
                    running,
                    "writing_context_manifest",
                    manifest_stored,
                    version,
                )
                self._register_frozen_snapshot(
                    connection,
                    project,
                    running,
                    "prompt_snapshot",
                    prompt_stored,
                    prompt.version,
                )
                self._register_frozen_snapshot(
                    connection,
                    project,
                    running,
                    "writing_contract_snapshot",
                    contract_stored,
                    contract.version,
                )
                self._register_frozen_snapshot(
                    connection,
                    project,
                    running,
                    "writing_request_prompt_snapshot",
                    request_stored,
                    request_prompt.version,
                )
                projection_repository = AcademicProjectionRepository(connection)
                if projection_manifest_stored is not None:
                    projection_manifest_artifact = self.persistence.register_artifact(
                        connection,
                        project,
                        running,
                        "writing_academic_projection_manifest",
                        projection_manifest_stored,
                        version,
                    )
                    projection_repository.add_manifest(
                        context_id=context.id,
                        project_id=project.id,
                        manifest_sha256=projection_manifest_stored.sha256,
                        manifest_artifact_id=projection_manifest_artifact.id,
                        created_at=utc_now(),
                    )
                for projection, stored in projection_stored:
                    artifact = self.persistence.register_artifact(
                        connection,
                        project,
                        running,
                        f"writing_academic_projection_{projection.unit_id}",
                        stored,
                        version,
                    )
                    projection_repository.add(
                        context_id=context.id,
                        project_id=project.id,
                        projection=projection,
                        plan_document_id=inputs.plan.document.id,
                        plan_artifact_id=inputs.plan.artifact.id,
                        plan_sha256=inputs.plan.artifact.sha256,
                        artifact_id=artifact.id,
                        created_at=utc_now(),
                    )
                context = repository.set_artifacts(
                    context, package_artifact.id, manifest_artifact.id
                )
                self.persistence.finish(connection, running.id)
            return context

    def latest(self, project_id: str) -> WritingContext:
        with self.database.connection() as connection:
            ProjectRepository(connection).get(project_id)
            context = WritingContextRepository(connection).latest(project_id)
            if context is None:
                raise NotFoundError(
                    "WRITING_CONTEXT_NOT_FOUND", "No WritingContext exists for this project"
                )
            return context

    def get(self, project_id: str, version: int | None = None) -> WritingContext:
        with self.database.connection() as connection:
            repository = WritingContextRepository(connection)
            context = (
                repository.latest(project_id)
                if version is None
                else repository.by_version(project_id, version)
            )
            if context is None:
                raise NotFoundError(
                    "WRITING_CONTEXT_NOT_FOUND", "Requested WritingContext was not found"
                )
            return context

    def get_by_id(self, project_id: str, context_id: str) -> WritingContext:
        """Resolve one immutable context without consulting the latest pointer."""
        with self.database.connection() as connection:
            ProjectRepository(connection).get(project_id)
            context = WritingContextRepository(connection).get(context_id)
            if context.project_id != project_id:
                raise NotFoundError(
                    "WRITING_CONTEXT_NOT_FOUND", "Requested WritingContext was not found"
                )
            return context

    def package(self, project_id: str, version: int | None = None) -> tuple[WritingContext, bytes]:
        context = self.get(project_id, version)
        return self.package_by_id(project_id, context.id)

    def package_by_id(self, project_id: str, context_id: str) -> tuple[WritingContext, bytes]:
        with self.database.connection() as connection:
            project = ProjectRepository(connection).get(project_id)
            context = WritingContextRepository(connection).get(context_id)
            if context.project_id != project.id:
                raise NotFoundError(
                    "WRITING_CONTEXT_NOT_FOUND", "Requested WritingContext was not found"
                )
            if context.package_artifact_id is None:
                raise IntegrityError(
                    "WRITING_CONTEXT_ARTIFACT_MISSING", "Context package is not registered"
                )
            return context, self.persistence.read_artifact(
                connection,
                project,
                context.package_artifact_id,
                expected_sha256=context.package_sha256,
            )

    def acknowledge(self, project_id: str, raw: bytes) -> WritingAcknowledgement:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WritingValidationError(
                "WRITING_ACKNOWLEDGEMENT_ENCODING_INVALID",
                "Acknowledgement must be valid UTF-8",
            ) from exc
        if not text.strip():
            raise WritingValidationError(
                "WRITING_ACKNOWLEDGEMENT_EMPTY", "Acknowledgement must not be empty"
            )
        try:
            context = self.latest(project_id)
        except NotFoundError as exc:
            raise ConflictError(
                "WRITING_CONTEXT_REQUIRED", "Create a WritingContext before acknowledgement"
            ) from exc
        return self.acknowledge_for_context(project_id, context.id, raw)

    def acknowledge_for_context(
        self, project_id: str, context_id: str, raw: bytes
    ) -> WritingAcknowledgement:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WritingValidationError(
                "WRITING_ACKNOWLEDGEMENT_ENCODING_INVALID",
                "Acknowledgement must be valid UTF-8",
            ) from exc
        if not text.strip():
            raise WritingValidationError(
                "WRITING_ACKNOWLEDGEMENT_EMPTY", "Acknowledgement must not be empty"
            )
        with self.database.connection() as connection:
            project = ProjectRepository(connection).get(project_id)
            context = WritingContextRepository(connection).get(context_id)
            if context.project_id != project.id:
                raise NotFoundError(
                    "WRITING_CONTEXT_NOT_FOUND", "Requested WritingContext was not found"
                )
            digest = sha256_bytes(raw)
            repository = AcknowledgementRepository(connection)
            existing = repository.by_hash(context.id, digest)
            if existing is not None:
                run = StageRunRepository(connection).get(existing.stage_run_id)
                if run.status is not RunStatus.DONE:
                    raise IntegrityError(
                        "WRITING_RECOVERY_REQUIRED", "Acknowledgement import needs recovery"
                    )
                return existing
            latest = repository.latest(context.id)
            raw_version = 1 if latest is None else latest.raw_version + 1
            input_hash = canonical_hash(
                {
                    "context_id": context.id,
                    "operation": "writing.context.acknowledge",
                    "raw_sha256": digest,
                }
            )
            run = self.persistence.new_run(
                project_id=project.id,
                stage_id=CONTEXT_STAGE,
                unit_id=f"context:ack:{context.id}",
                input_hash=input_hash,
                version=raw_version,
            )
            item = WritingAcknowledgement(
                new_id(), project.id, context.id, run.id, raw_version, digest, None, None, utc_now()
            )
            with self.database.transaction(connection):
                running = self.persistence.add_and_start(connection, run)
                repository.add(item)
            stored = self.store.write_bytes(
                project.artifact_root,
                f"text/context/v{context.version:04d}/acknowledgement/raw-v{raw_version:04d}.txt",
                raw,
            )
            self.persistence.checkpoint("acknowledgement.raw")
            with self.database.transaction(connection):
                artifact = self.persistence.register_artifact(
                    connection,
                    project,
                    running,
                    "writing_context_acknowledgement_raw",
                    stored,
                    raw_version,
                )
                item = repository.set_artifact(item, artifact.id)
                self.persistence.finish(connection, running.id)
            return item

    def confirm(self, project_id: str, raw_version: int | None = None) -> WritingAcknowledgement:
        try:
            context = self.latest(project_id)
        except NotFoundError as exc:
            raise ConflictError("WRITING_CONTEXT_REQUIRED", "No WritingContext exists") from exc
        return self.confirm_for_context(project_id, context.id, raw_version)

    def confirm_for_context(
        self, project_id: str, context_id: str, raw_version: int | None = None
    ) -> WritingAcknowledgement:
        with self.database.connection() as connection:
            project = ProjectRepository(connection).get(project_id)
            context = WritingContextRepository(connection).get(context_id)
            if context.project_id != project.id:
                raise NotFoundError(
                    "WRITING_CONTEXT_NOT_FOUND", "Requested WritingContext was not found"
                )
            repository = AcknowledgementRepository(connection)
            item = (
                repository.latest(context.id)
                if raw_version is None
                else repository.by_raw_version(context.id, raw_version)
            )
            if item is None:
                raise NotFoundError(
                    "WRITING_ACKNOWLEDGEMENT_NOT_FOUND", "Acknowledgement was not found"
                )
            if item.confirmed_at is not None:
                return item
            if item.raw_artifact_id is None:
                raise IntegrityError(
                    "WRITING_ACKNOWLEDGEMENT_ARTIFACT_MISSING",
                    "Acknowledgement raw artifact is not registered",
                )
            self.persistence.read_artifact(
                connection, project, item.raw_artifact_id, expected_sha256=item.raw_sha256
            )
            input_hash = canonical_hash(
                {
                    "acknowledgement_id": item.id,
                    "operation": "writing.context.confirm",
                    "raw_sha256": item.raw_sha256,
                }
            )
            runs = StageRunRepository(connection)
            existing = runs.find_identity(
                project.id, CONTEXT_STAGE, f"context:confirm:{item.id}", input_hash
            )
            if existing is not None and existing.status is RunStatus.DONE:
                return repository.get(item.id)
            run = self.persistence.new_run(
                project_id=project.id,
                stage_id=CONTEXT_STAGE,
                unit_id=f"context:confirm:{item.id}",
                input_hash=input_hash,
                version=1,
            )
            with self.database.transaction(connection):
                running = self.persistence.add_and_start(connection, run)
            with self.database.transaction(connection):
                item = repository.confirm(item, utc_now())
            self.persistence.checkpoint("acknowledgement.confirmed")
            with self.database.transaction(connection):
                self.persistence.finish(connection, running.id)
            return item

    def is_confirmed(self, connection, context_id: str) -> bool:  # type: ignore[no-untyped-def]
        return AcknowledgementRepository(connection).confirmed(context_id) is not None

    def is_current(self, context: WritingContext) -> bool:
        inputs = self.academic.get_current_writing_inputs(context.project_id)
        return (
            inputs is not None
            and inputs.answers.document.id == context.answers_document_id
            and inputs.answers.artifact.id == context.answers_artifact_id
            and inputs.answers.artifact.sha256 == context.answers_sha256
            and inputs.plan.document.id == context.plan_document_id
            and inputs.plan.artifact.id == context.plan_artifact_id
            and inputs.plan.artifact.sha256 == context.plan_sha256
            and inputs.plan.document.upstream_document_id == inputs.answers.document.id
        )

    def resolve_contract(self, context: WritingContext) -> ResolvedWritingContract:
        contract = self.contracts.resolve(context.contract_id, context.contract_version)
        if contract.sha256 != context.contract_sha256:
            raise IntegrityError(
                "WRITING_CONTRACT_BINDING_INVALID", "Context contract binding differs"
            )
        return contract

    def payload(self, context: WritingContext) -> dict[str, object]:
        with self.database.connection() as connection:
            acknowledgement = AcknowledgementRepository(connection).confirmed(context.id)
        return {
            **asdict(context),
            "confirmed": acknowledgement is not None,
            "current": self.is_current(context),
        }

    def _assert_inputs_still_current(self, expected: CurrentWritingInputs) -> None:
        """Recheck M1 through its public API while the caller holds the SQLite write lock."""
        current = self.academic.get_current_writing_inputs(expected.answers.document.project_id)
        if current is None or (
            current.answers.document.id,
            current.answers.artifact.id,
            current.answers.artifact.sha256,
            current.plan.document.id,
            current.plan.artifact.id,
            current.plan.artifact.sha256,
            current.plan.document.upstream_document_id,
        ) != (
            expected.answers.document.id,
            expected.answers.artifact.id,
            expected.answers.artifact.sha256,
            expected.plan.document.id,
            expected.plan.artifact.id,
            expected.plan.artifact.sha256,
            expected.plan.document.upstream_document_id,
        ):
            raise ConflictError(
                "WRITING_ACADEMIC_INPUTS_CHANGED",
                "Consolidated Answers or Academic Plan changed during context creation",
            )

    def _register_frozen_snapshot(
        self,
        connection: sqlite3.Connection,
        project: Project,
        run: StageRun,
        artifact_type: str,
        stored: StoredFile,
        version: int,
    ) -> None:
        existing = ArtifactRepository(connection).get_by_path(
            project.id, stored.relative_path
        )
        if existing is not None:
            if existing.sha256 != stored.sha256 or existing.byte_size != stored.byte_size:
                raise IntegrityError(
                    "WRITING_FROZEN_SNAPSHOT_CONFLICT",
                    f"Frozen snapshot {stored.relative_path!r} differs from its artifact",
                )
            return
        self.persistence.register_artifact(
            connection, project, run, artifact_type, stored, version
        )

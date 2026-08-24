from __future__ import annotations

from dataclasses import dataclass

from ebook_pipeline.config import AppConfig
from ebook_pipeline.core.errors import ConflictError, IntegrityError, NotFoundError
from ebook_pipeline.core.hashing import canonical_hash, sha256_bytes
from ebook_pipeline.core.ids import new_id, utc_now
from ebook_pipeline.core.models import RunStatus
from ebook_pipeline.prompts import PromptRegistry, ResolvedPrompt
from ebook_pipeline.storage.artifacts import ArtifactStore
from ebook_pipeline.storage.database import Database
from ebook_pipeline.storage.repositories import ProjectRepository, StageRunRepository
from ebook_pipeline.writing.context import WritingContextManager
from ebook_pipeline.writing.contracts import ResolvedWritingContract, WritingUnitContract
from ebook_pipeline.writing.models import (
    PreparationDependency,
    ProductionUnitStatus,
    WritingContext,
    WritingUnitPreparation,
)
from ebook_pipeline.writing.persistence import FaultHook, WritingPersistence
from ebook_pipeline.writing.production_set import ProductionSetResolver
from ebook_pipeline.writing.projections import (
    AcademicProjectionRepository,
    StoredAcademicProjection,
)
from ebook_pipeline.writing.rendering import (
    preparation_manifest,
    render_continuity,
    render_unit_request,
)
from ebook_pipeline.writing.repositories import (
    AcknowledgementRepository,
    PreparationRepository,
)

TEXT_STAGE = "text_generation"


@dataclass(frozen=True, slots=True)
class PreparationOutputs:
    request: bytes
    manifest: bytes
    request_sha256: str
    manifest_sha256: str
    input_hash: str


def build_preparation_outputs(
    *,
    project_id: str,
    context: WritingContext,
    contract: ResolvedWritingContract,
    unit: WritingUnitContract,
    request_prompt: ResolvedPrompt,
    dependencies: tuple[PreparationDependency, ...],
    continuity_contents: tuple[tuple[PreparationDependency, bytes], ...],
    projection: StoredAcademicProjection | None,
    academic_context: bytes | None,
) -> PreparationOutputs:
    allow_lists = (
        contract.model.validation.allow_lists if unit.allow_lists is None else unit.allow_lists
    )
    request = render_unit_request(
        request_prompt,
        unit,
        allow_lists=allow_lists,
        continuity=render_continuity(continuity_contents),
        academic_context=academic_context,
    )
    request_sha = sha256_bytes(request)
    manifest = preparation_manifest(
        project_id=project_id,
        context_id=context.id,
        unit=unit,
        contract=contract,
        request_prompt=request_prompt,
        request_sha256=request_sha,
        dependencies=dependencies,
        academic_projection=projection,
    )
    manifest_sha = sha256_bytes(manifest)
    input_hash = canonical_hash(
        {
            "context_id": context.id,
            "contract_sha256": contract.sha256,
            "continuity": [
                (
                    item.dependency_unit_id,
                    item.submission_id,
                    item.accepted_version,
                    item.artifact_id,
                    item.sha256,
                )
                for item in dependencies
            ],
            "operation": "writing.unit.prepare",
            "academic_context": (
                None
                if projection is None
                else (
                    projection.artifact_id,
                    projection.projection_version,
                    projection.academic_context_sha256,
                )
            ),
            "request_prompt_sha256": request_prompt.sha256,
            "request_sha256": request_sha,
            "unit": unit.model_dump(mode="json"),
        }
    )
    return PreparationOutputs(request, manifest, request_sha, manifest_sha, input_hash)


class WritingPreparationManager:
    def __init__(
        self,
        config: AppConfig,
        database: Database,
        store: ArtifactStore,
        contexts: WritingContextManager,
        fault_hook: FaultHook | None = None,
    ) -> None:
        self.config = config
        self.database = database
        self.store = store
        self.contexts = contexts
        self.prompts = PromptRegistry(config.prompt_registry)
        self.persistence = WritingPersistence(config, database, store, fault_hook)
        self.resolver = ProductionSetResolver()

    def prepare(
        self, project_id: str, unit_id: str, *, reprocess: bool = False
    ) -> WritingUnitPreparation:
        context = self.contexts.latest(project_id)
        return self.prepare_for_context(project_id, context.id, unit_id, reprocess=reprocess)

    def prepare_for_context(
        self,
        project_id: str,
        context_id: str,
        unit_id: str,
        *,
        reprocess: bool = False,
    ) -> WritingUnitPreparation:
        context = self.contexts.get_by_id(project_id, context_id)
        if not self.contexts.is_current(context):
            raise ConflictError(
                "WRITING_CONTEXT_STALE",
                "Latest WritingContext is not current with M1 Answers and Academic Plan",
            )
        contract = self.contexts.resolve_contract(context)
        unit = contract.model.unit(unit_id)
        request_prompt = self.prompts.resolve(
            context.request_prompt_id, context.request_prompt_version
        )
        if request_prompt.sha256 != context.request_prompt_sha256:
            raise IntegrityError(
                "WRITING_REQUEST_PROMPT_BINDING_INVALID",
                "Context unit request prompt binding differs",
            )
        with self.database.connection() as connection:
            project = ProjectRepository(connection).get(project_id)
            if AcknowledgementRepository(connection).confirmed(context.id) is None:
                raise ConflictError(
                    "WRITING_CONTEXT_NOT_CONFIRMED",
                    "Confirm the context acknowledgement before preparing a unit",
                )
            production_set = self.resolver.resolve(
                connection,
                project_id=project.id,
                context_id=context.id,
                contract=contract,
                context_current=True,
            )
            selection_map = {
                selection.unit_id: selection for selection in production_set.selections
            }
            preparation_id = new_id()
            dependencies: list[PreparationDependency] = []
            continuity_contents: list[tuple[PreparationDependency, bytes]] = []
            for dependency_id in unit.continuity_from:
                selection = selection_map[dependency_id]
                if (
                    selection.status is not ProductionUnitStatus.CURRENT_COMPATIBLE
                    or selection.submission is None
                    or selection.submission.accepted_version is None
                    or selection.accepted_artifact_id is None
                    or selection.accepted_sha256 is None
                ):
                    raise ConflictError(
                        "WRITING_CONTINUITY_DEPENDENCY_UNAVAILABLE",
                        f"Dependency {dependency_id!r} has no current-compatible "
                        "accepted submission",
                    )
                dependency = PreparationDependency(
                    preparation_id=preparation_id,
                    project_id=project.id,
                    context_id=context.id,
                    dependency_unit_id=dependency_id,
                    submission_id=selection.submission.id,
                    accepted_version=selection.submission.accepted_version,
                    artifact_id=selection.accepted_artifact_id,
                    sha256=selection.accepted_sha256,
                )
                content = self.persistence.read_artifact(
                    connection,
                    project,
                    dependency.artifact_id,
                    expected_sha256=dependency.sha256,
                )
                dependencies.append(dependency)
                continuity_contents.append((dependency, content))
            academic_context = None
            projection = AcademicProjectionRepository(connection).get(context.id, unit.unit_id)
            if projection is not None:
                academic_context = self.persistence.read_artifact(
                    connection,
                    project,
                    projection.artifact_id,
                    expected_sha256=projection.academic_context_sha256,
                )
            outputs = build_preparation_outputs(
                project_id=project.id,
                context=context,
                unit=unit,
                contract=contract,
                request_prompt=request_prompt,
                dependencies=tuple(dependencies),
                continuity_contents=tuple(continuity_contents),
                projection=projection,
                academic_context=academic_context,
            )
            repository = PreparationRepository(connection)
            existing = repository.by_input(context.id, unit.unit_id, outputs.input_hash)
            if existing is not None and not reprocess:
                run = StageRunRepository(connection).get(existing.stage_run_id)
                if run.status is not RunStatus.DONE:
                    raise IntegrityError(
                        "WRITING_RECOVERY_REQUIRED", "Unit preparation needs recovery"
                    )
                return existing
            with self.database.transaction(connection):
                latest = repository.latest_for_project_unit(project.id, unit.unit_id)
                version = 1 if latest is None else latest.version + 1
                run = self.persistence.new_run(
                    project_id=project.id,
                    stage_id=TEXT_STAGE,
                    unit_id=f"prepare:{unit.unit_id}",
                    input_hash=outputs.input_hash,
                    version=version,
                    supersedes_run_id=None if latest is None else latest.stage_run_id,
                )
                item = WritingUnitPreparation(
                    id=preparation_id,
                    project_id=project.id,
                    context_id=context.id,
                    stage_run_id=run.id,
                    unit_id=unit.unit_id,
                    version=version,
                    input_hash=outputs.input_hash,
                    request_sha256=outputs.request_sha256,
                    manifest_sha256=outputs.manifest_sha256,
                    request_artifact_id=None,
                    manifest_artifact_id=None,
                    created_at=utc_now(),
                )
                running = self.persistence.add_and_start(connection, run)
                repository.add(item)
                repository.add_dependencies(tuple(dependencies))
            request_stored = self.store.write_bytes(
                project.artifact_root,
                f"text/preparations/{unit.unit_id}/v{version:04d}/request.txt",
                outputs.request,
            )
            self.persistence.checkpoint("preparation.request")
            manifest_stored = self.store.write_bytes(
                project.artifact_root,
                f"text/preparations/{unit.unit_id}/v{version:04d}/manifest.json",
                outputs.manifest,
            )
            self.persistence.checkpoint("preparation.manifest")
            with self.database.transaction(connection):
                request_artifact = self.persistence.register_artifact(
                    connection,
                    project,
                    running,
                    "writing_unit_request",
                    request_stored,
                    version,
                )
                manifest_artifact = self.persistence.register_artifact(
                    connection,
                    project,
                    running,
                    "writing_continuity_manifest",
                    manifest_stored,
                    version,
                )
                item = repository.set_artifacts(item, request_artifact.id, manifest_artifact.id)
                self.persistence.finish(connection, running.id)
            return item

    def latest(self, project_id: str, unit_id: str) -> WritingUnitPreparation:
        context = self.contexts.latest(project_id)
        with self.database.connection() as connection:
            item = PreparationRepository(connection).latest(context.id, unit_id)
            if item is None:
                raise NotFoundError(
                    "WRITING_PREPARATION_NOT_FOUND", f"No preparation exists for {unit_id!r}"
                )
            return item

    def request(self, project_id: str, unit_id: str) -> tuple[WritingUnitPreparation, bytes]:
        item = self.latest(project_id, unit_id)
        return self.request_by_id(project_id, item.id)

    def request_by_id(
        self, project_id: str, preparation_id: str
    ) -> tuple[WritingUnitPreparation, bytes]:
        with self.database.connection() as connection:
            project = ProjectRepository(connection).get(project_id)
            item = PreparationRepository(connection).get(preparation_id)
            if item.project_id != project.id or item.request_artifact_id is None:
                raise NotFoundError(
                    "WRITING_PREPARATION_NOT_FOUND",
                    "No complete preparation exists for the requested identity",
                )
            run = StageRunRepository(connection).get(item.stage_run_id)
            if run.status is not RunStatus.DONE:
                raise IntegrityError(
                    "WRITING_PREPARATION_INCOMPLETE", "Unit preparation is not complete"
                )
            return item, self.persistence.read_artifact(
                connection,
                project,
                item.request_artifact_id,
                expected_sha256=item.request_sha256,
            )

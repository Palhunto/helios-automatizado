from __future__ import annotations

from ebook_pipeline.config import AppConfig
from ebook_pipeline.core.errors import ConflictError, IntegrityError, NotFoundError
from ebook_pipeline.core.hashing import canonical_json_bytes, sha256_bytes
from ebook_pipeline.core.ids import new_id, utc_now
from ebook_pipeline.core.models import RunStatus
from ebook_pipeline.storage.artifacts import ArtifactStore
from ebook_pipeline.storage.database import Database
from ebook_pipeline.storage.repositories import ProjectRepository, StageRunRepository
from ebook_pipeline.writing.context import WritingContextManager
from ebook_pipeline.writing.models import ProductionUnitStatus, TextConsolidation
from ebook_pipeline.writing.persistence import FaultHook, WritingPersistence
from ebook_pipeline.writing.production_set import ProductionSetResolver
from ebook_pipeline.writing.repositories import ConsolidationRepository

CONSOLIDATION_STAGE = "text_consolidation"


class TextConsolidator:
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
        self.persistence = WritingPersistence(config, database, store, fault_hook)
        self.resolver = ProductionSetResolver()

    def consolidate(
        self, project_id: str, *, allow_historical_context: bool = False
    ) -> TextConsolidation:
        context = self.contexts.latest(project_id)
        context_current = self.contexts.is_current(context)
        if not context_current and not allow_historical_context:
            raise ConflictError(
                "WRITING_CONTEXT_STALE",
                "Normal consolidation requires a current WritingContext",
            )
        contract = self.contexts.resolve_contract(context)
        with self.database.connection() as connection:
            project = ProjectRepository(connection).get(project_id)
            production_set = self.resolver.resolve(
                connection,
                project_id=project.id,
                context_id=context.id,
                contract=contract,
                context_current=context_current,
            )
            if not production_set.complete or production_set.production_set_hash is None:
                missing = [
                    selection.unit_id
                    for selection in production_set.selections
                    if selection.status is not ProductionUnitStatus.CURRENT_COMPATIBLE
                ]
                raise ConflictError(
                    "TEXT_PRODUCTION_SET_INCOMPLETE",
                    "Consolidation requires every contract unit to be dependency-compatible",
                    evidence={"units": missing},
                )
            repository = ConsolidationRepository(connection)
            existing = repository.by_set_hash(context.id, production_set.production_set_hash)
            if existing is not None:
                run = StageRunRepository(connection).get(existing.stage_run_id)
                if run.status is not RunStatus.DONE:
                    raise IntegrityError(
                        "WRITING_RECOVERY_REQUIRED", "Text consolidation needs recovery"
                    )
                return existing
            chunks: list[bytes] = []
            manifest_units: list[dict[str, object]] = []
            for selection in production_set.selections:
                assert selection.submission is not None
                assert selection.submission.accepted_version is not None
                assert selection.accepted_artifact_id is not None
                assert selection.accepted_sha256 is not None
                chunks.append(
                    self.persistence.read_artifact(
                        connection,
                        project,
                        selection.accepted_artifact_id,
                        expected_sha256=selection.accepted_sha256,
                    )
                )
                manifest_units.append(
                    {
                        "accepted_version": selection.submission.accepted_version,
                        "artifact_id": selection.accepted_artifact_id,
                        "sha256": selection.accepted_sha256,
                        "submission_id": selection.submission.id,
                        "unit_id": selection.unit_id,
                    }
                )
            text = contract.model.separator.encode("utf-8").join(chunks)
            text_sha = sha256_bytes(text)
            manifest = (
                canonical_json_bytes(
                    {
                        "context_id": context.id,
                        "contract": {
                            "id": contract.id,
                            "sha256": contract.sha256,
                            "version": contract.version,
                        },
                        "production_set_hash": production_set.production_set_hash,
                        "project_id": project.id,
                        "separator": contract.model.separator,
                        "text_sha256": text_sha,
                        "units": manifest_units,
                    }
                )
                + b"\n"
            )
            manifest_sha = sha256_bytes(manifest)
            latest = repository.latest(project.id)
            version = 1 if latest is None else latest.version + 1
            run = self.persistence.new_run(
                project_id=project.id,
                stage_id=CONSOLIDATION_STAGE,
                unit_id="consolidate",
                input_hash=production_set.production_set_hash,
                version=version,
                supersedes_run_id=None if latest is None else latest.stage_run_id,
            )
            item = TextConsolidation(
                id=new_id(),
                project_id=project.id,
                context_id=context.id,
                stage_run_id=run.id,
                version=version,
                production_set_hash=production_set.production_set_hash,
                separator=contract.model.separator,
                text_sha256=text_sha,
                manifest_sha256=manifest_sha,
                text_artifact_id=None,
                manifest_artifact_id=None,
                created_at=utc_now(),
            )
            with self.database.transaction(connection):
                running = self.persistence.add_and_start(connection, run)
                repository.add(item)
            text_stored = self.store.write_bytes(
                project.artifact_root, f"text/consolidated/v{version:04d}.txt", text
            )
            self.persistence.checkpoint("consolidation.text")
            manifest_stored = self.store.write_bytes(
                project.artifact_root,
                f"text/consolidated/v{version:04d}.manifest.json",
                manifest,
            )
            self.persistence.checkpoint("consolidation.manifest")
            with self.database.transaction(connection):
                text_artifact = self.persistence.register_artifact(
                    connection,
                    project,
                    running,
                    "text_consolidated",
                    text_stored,
                    version,
                )
                manifest_artifact = self.persistence.register_artifact(
                    connection,
                    project,
                    running,
                    "text_consolidation_manifest",
                    manifest_stored,
                    version,
                )
                item = repository.set_artifacts(item, text_artifact.id, manifest_artifact.id)
                for order, selection in enumerate(production_set.selections, start=1):
                    assert selection.submission is not None
                    assert selection.accepted_artifact_id is not None
                    assert selection.accepted_sha256 is not None
                    repository.add_member(
                        consolidation=item,
                        unit_id=selection.unit_id,
                        unit_order=order,
                        submission=selection.submission,
                        artifact_id=selection.accepted_artifact_id,
                        sha256=selection.accepted_sha256,
                    )
                self.persistence.finish(connection, running.id)
            return item

    def show(self, project_id: str, version: int | None = None) -> tuple[TextConsolidation, bytes]:
        with self.database.connection() as connection:
            project = ProjectRepository(connection).get(project_id)
            repository = ConsolidationRepository(connection)
            item = repository.latest(project_id)
            if version is not None:
                row = connection.execute(
                    "SELECT * FROM text_consolidations WHERE project_id = ? AND version = ?",
                    (project_id, version),
                ).fetchone()
                item = None if row is None else repository.get(str(row["id"]))
            if item is None or item.text_artifact_id is None:
                raise NotFoundError(
                    "TEXT_CONSOLIDATION_NOT_FOUND", "No complete text consolidation was found"
                )
            return item, self.persistence.read_artifact(
                connection,
                project,
                item.text_artifact_id,
                expected_sha256=item.text_sha256,
            )

from __future__ import annotations

import sqlite3
from collections.abc import Callable

from ebook_pipeline.config import AppConfig
from ebook_pipeline.core.errors import ConflictError, IntegrityError
from ebook_pipeline.core.ids import new_id, utc_now
from ebook_pipeline.core.models import Artifact, Project, RunStatus, StageRun, StoredFile
from ebook_pipeline.core.state_machine import StateMachine
from ebook_pipeline.storage.artifacts import ArtifactStore
from ebook_pipeline.storage.database import Database
from ebook_pipeline.storage.repositories import ArtifactRepository, StageRunRepository
from ebook_pipeline.writing.repositories import WritingArtifactRepository

FaultHook = Callable[[str], None]


class WritingPersistence:
    def __init__(
        self,
        config: AppConfig,
        database: Database,
        store: ArtifactStore,
        fault_hook: FaultHook | None = None,
    ) -> None:
        self.config = config
        self.database = database
        self.store = store
        self.state_machine = StateMachine()
        self.fault_hook = fault_hook

    def checkpoint(self, name: str) -> None:
        if self.fault_hook is not None:
            self.fault_hook(name)

    def new_run(
        self,
        *,
        project_id: str,
        stage_id: str,
        unit_id: str,
        input_hash: str,
        version: int,
        supersedes_run_id: str | None = None,
    ) -> StageRun:
        return self.state_machine.new_run(
            project_id=project_id,
            stage_id=stage_id,
            unit_id=unit_id,
            input_hash=input_hash,
            max_attempts=self.config.max_attempts,
            version=version,
            supersedes_run_id=supersedes_run_id,
        )

    def add_and_start(self, connection: sqlite3.Connection, run: StageRun) -> StageRun:
        runs = StageRunRepository(connection)
        runs.add(run)
        running = self.state_machine.transition(run, RunStatus.RUNNING)
        runs.update(running, expected_status=run.status)
        return running

    def finish(self, connection: sqlite3.Connection, run_id: str) -> StageRun:
        runs = StageRunRepository(connection)
        current = runs.get(run_id)
        if current.status is RunStatus.DONE:
            return current
        done = self.state_machine.transition(current, RunStatus.DONE)
        runs.update(done, expected_status=current.status)
        return done

    def register_artifact(
        self,
        connection: sqlite3.Connection,
        project: Project,
        run: StageRun,
        artifact_type: str,
        stored: StoredFile,
        version: int,
    ) -> Artifact:
        return ArtifactRepository(connection).add_idempotent(
            Artifact(
                id=new_id(),
                project_id=project.id,
                stage_run_id=run.id,
                artifact_type=artifact_type,
                relative_path=stored.relative_path,
                sha256=stored.sha256,
                byte_size=stored.byte_size,
                version=version,
                created_at=utc_now(),
            )
        )

    def read_artifact(
        self,
        connection: sqlite3.Connection,
        project: Project,
        artifact_id: str,
        *,
        expected_sha256: str | None = None,
    ) -> bytes:
        artifact = WritingArtifactRepository(connection).get(artifact_id)
        if artifact.project_id != project.id:
            raise IntegrityError(
                "WRITING_ARTIFACT_PROJECT_MISMATCH", "Artifact belongs to another project"
            )
        inspected = self.store.inspect(project.artifact_root, artifact.relative_path)
        if inspected.sha256 != artifact.sha256 or inspected.byte_size != artifact.byte_size:
            raise IntegrityError(
                "WRITING_ARTIFACT_HASH_MISMATCH",
                f"Writing artifact {artifact.relative_path!r} differs from its record",
            )
        if expected_sha256 is not None and artifact.sha256 != expected_sha256:
            raise IntegrityError(
                "WRITING_PROVENANCE_HASH_MISMATCH",
                f"Writing artifact {artifact.relative_path!r} differs from provenance",
            )
        return self.store.resolve(project.artifact_root, artifact.relative_path).read_bytes()

    def verify_stored(self, stored: StoredFile, expected_sha256: str) -> None:
        if stored.sha256 != expected_sha256:
            raise ConflictError(
                "WRITING_OUTPUT_HASH_CONFLICT", "Stored writing output differs from expected bytes"
            )

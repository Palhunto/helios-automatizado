from __future__ import annotations

import json
import sqlite3

from ebook_pipeline.academic.recovery import AcademicRecovery
from ebook_pipeline.academic.service import AcademicService
from ebook_pipeline.config import AppConfig, load_project_config
from ebook_pipeline.core.errors import HeliosError
from ebook_pipeline.core.hashing import sha256_file
from ebook_pipeline.core.ids import new_id, utc_now
from ebook_pipeline.core.models import Artifact, Project, RecoveryResult, RunStatus, StageRun
from ebook_pipeline.core.projects import CREATE_STAGE
from ebook_pipeline.core.state_machine import StateMachine
from ebook_pipeline.storage.artifacts import ArtifactStore
from ebook_pipeline.storage.database import Database
from ebook_pipeline.storage.repositories import (
    ArtifactRepository,
    ErrorRepository,
    ProjectRepository,
    StageRunRepository,
)
from ebook_pipeline.writing.context import WritingContextManager
from ebook_pipeline.writing.recovery import WritingRecovery


class RecoveryService:
    def __init__(self, config: AppConfig, database: Database, store: ArtifactStore) -> None:
        self.config = config
        self.database = database
        self.store = store
        self.state_machine = StateMachine()

    def recover(self, project_id: str) -> list[RecoveryResult]:
        with self.database.connection() as connection:
            project = ProjectRepository(connection).get(project_id)
            running = StageRunRepository(connection).list_running(project_id)
            return [self._recover_run(connection, project, run) for run in running]

    def _recover_run(
        self, connection: sqlite3.Connection, project: Project, run: StageRun
    ) -> RecoveryResult:
        academic_result = AcademicRecovery(self.config, self.database, self.store).recover_run(
            connection, project, run
        )
        if academic_result is not None:
            return academic_result
        writing_contexts = WritingContextManager(
            self.config,
            self.database,
            self.store,
            AcademicService(self.config, self.database, self.store),
        )
        writing_result = WritingRecovery(
            self.config, self.database, self.store, writing_contexts
        ).recover_run(connection, project, run)
        if writing_result is not None:
            return writing_result
        if run.stage_id == "chatgpt_browser_automation":
            return RecoveryResult(
                run.id,
                run.status,
                run.status,
                "External browser effect requires `ebook browser recover`; no retry was inferred",
            )
        if run.stage_id != CREATE_STAGE:
            return self._pending_retry(
                connection,
                run,
                "ORPHANED_RUN",
                "No M0 reconciler exists for this stage; it is safe to retry",
            )
        try:
            config_path = self.store.resolve(project.artifact_root, project.config_path)
            manifest_path = self.store.resolve(project.artifact_root, "project.json")
            if not config_path.is_file() or not manifest_path.is_file():
                return self._pending_retry(
                    connection,
                    run,
                    "CREATE_ARTIFACT_MISSING",
                    "Project creation artifacts are incomplete",
                )
            loaded = load_project_config(config_path)
            manifest_value = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(manifest_value, dict):
                raise ValueError("project manifest must be an object")
            expected = {
                "id": project.id,
                "name": project.name,
                "slug": project.slug,
                "artifact_root": project.artifact_root,
                "config_path": project.config_path,
                "input_hash": run.input_hash,
                "config_sha256": sha256_file(config_path),
                "created_at": project.created_at,
            }
            if manifest_value != expected or loaded.input_hash != run.input_hash:
                raise ValueError(
                    "project manifest or normalized config hash does not match the run"
                )
            config_stored = self.store.inspect(project.artifact_root, project.config_path)
            manifest_stored = self.store.inspect(project.artifact_root, "project.json")
        except (HeliosError, OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            return self._blocked(
                connection,
                run,
                "CREATE_ARTIFACT_CORRUPT",
                f"Project creation artifacts cannot be trusted: {exc}",
            )

        repositories = ArtifactRepository(connection)
        runs = StageRunRepository(connection)
        with self.database.transaction(connection):
            for artifact_type, stored in (
                ("project_config", config_stored),
                ("project_manifest", manifest_stored),
            ):
                repositories.add_idempotent(
                    Artifact(
                        id=new_id(),
                        project_id=project.id,
                        stage_run_id=run.id,
                        artifact_type=artifact_type,
                        relative_path=stored.relative_path,
                        sha256=stored.sha256,
                        byte_size=stored.byte_size,
                        version=run.version,
                        created_at=utc_now(),
                    )
                )
            done = self.state_machine.transition(run, RunStatus.DONE, recovery=True)
            runs.update(done, expected_status=run.status)
        return RecoveryResult(run.id, run.status, done.status, "Artifacts reconciled")

    def _pending_retry(
        self,
        connection: sqlite3.Connection,
        run: StageRun,
        code: str,
        message: str,
    ) -> RecoveryResult:
        runs = StageRunRepository(connection)
        with self.database.transaction(connection):
            ErrorRepository(connection).add(
                project_id=run.project_id,
                stage_run_id=run.id,
                code=code,
                message=message,
                recoverable=True,
            )
            retry = self.state_machine.transition(run, RunStatus.PENDING_RETRY, recovery=True)
            runs.update(retry, expected_status=run.status)
        return RecoveryResult(run.id, run.status, retry.status, message)

    def _blocked(
        self,
        connection: sqlite3.Connection,
        run: StageRun,
        code: str,
        message: str,
    ) -> RecoveryResult:
        runs = StageRunRepository(connection)
        with self.database.transaction(connection):
            ErrorRepository(connection).add(
                project_id=run.project_id,
                stage_run_id=run.id,
                code=code,
                message=message,
                recoverable=False,
            )
            failed = self.state_machine.transition(run, RunStatus.FAILED)
            runs.update(failed, expected_status=run.status)
            blocked = self.state_machine.transition(failed, RunStatus.BLOCKED)
            runs.update(blocked, expected_status=failed.status)
        return RecoveryResult(run.id, run.status, blocked.status, message)

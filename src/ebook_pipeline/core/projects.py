from __future__ import annotations

import sqlite3
from pathlib import Path

from ebook_pipeline.config import AppConfig, LoadedProjectConfig, load_project_config
from ebook_pipeline.core.errors import ConflictError, HeliosError, IntegrityError
from ebook_pipeline.core.hashing import canonical_json_bytes, sha256_bytes
from ebook_pipeline.core.ids import new_id, utc_now
from ebook_pipeline.core.models import Artifact, Project, RunStatus, StageRun, ValidationIssue
from ebook_pipeline.core.run_evidence import DoneRunEvidencePolicy
from ebook_pipeline.core.runtime_config import ProjectRuntimeService
from ebook_pipeline.core.state_machine import StateMachine
from ebook_pipeline.storage.artifacts import ArtifactStore
from ebook_pipeline.storage.database import Database
from ebook_pipeline.storage.repositories import (
    ArtifactRepository,
    ErrorRepository,
    ProjectRepository,
    StageRunRepository,
)

CREATE_STAGE = "create_project"


def project_manifest(project: Project, loaded: LoadedProjectConfig) -> bytes:
    payload = {
        "artifact_root": project.artifact_root,
        "config_path": project.config_path,
        "config_sha256": sha256_bytes(loaded.raw_bytes),
        "created_at": project.created_at,
        "id": project.id,
        "input_hash": loaded.input_hash,
        "name": project.name,
        "slug": project.slug,
    }
    return canonical_json_bytes(payload) + b"\n"


class ProjectService:
    def __init__(self, config: AppConfig, database: Database, store: ArtifactStore) -> None:
        self.config = config
        self.database = database
        self.store = store
        self.state_machine = StateMachine()
        self.done_run_evidence = DoneRunEvidencePolicy()
        self.runtime_config = ProjectRuntimeService(config, database, store)

    def create(self, config_path: Path) -> Project:
        loaded = load_project_config(config_path)
        identity = loaded.model.project
        with self.database.connection() as connection:
            projects = ProjectRepository(connection)
            runs = StageRunRepository(connection)
            existing = projects.get_by_slug(identity.slug)
            if existing is not None:
                run = runs.find_identity(
                    existing.id, CREATE_STAGE, CREATE_STAGE, loaded.input_hash, version=1
                )
                if run is None:
                    raise ConflictError(
                        "PROJECT_SLUG_CONFLICT",
                        f"Slug {identity.slug!r} already belongs to different project inputs",
                    )
                if run.status is RunStatus.DONE:
                    issues = self._validate_with_connection(connection, existing)
                    if issues:
                        raise IntegrityError(
                            "PROJECT_INTEGRITY_FAILED",
                            f"Existing project has {len(issues)} integrity issue(s)",
                            evidence={"issues": [issue.code for issue in issues]},
                        )
                    return existing
                if run.status is RunStatus.RUNNING:
                    raise IntegrityError(
                        "PROJECT_RECOVERY_REQUIRED",
                        "Project creation was interrupted; run project recover first",
                        recoverable=True,
                    )
                run = self._prepare_retry(connection, run)
                project = existing
            else:
                timestamp = utc_now()
                project_id = new_id()
                project = Project(
                    id=project_id,
                    name=identity.name,
                    slug=identity.slug,
                    config_path="config/project.yaml",
                    artifact_root=f"{identity.slug}-{project_id}",
                    created_at=timestamp,
                    updated_at=timestamp,
                )
                run = self.state_machine.new_run(
                    project_id=project.id,
                    stage_id=CREATE_STAGE,
                    unit_id=CREATE_STAGE,
                    input_hash=loaded.input_hash,
                    max_attempts=self.config.max_attempts,
                )
                with self.database.transaction(connection):
                    projects.add(project)
                    runs.add(run)
                    running = self.state_machine.transition(run, RunStatus.RUNNING)
                    runs.update(running, expected_status=run.status)
                run = running

            try:
                self._materialize(connection, project, run, loaded)
            except HeliosError as exc:
                self._record_failure(connection, run.id, exc)
                raise
            except Exception as exc:
                error = IntegrityError(
                    "PROJECT_CREATE_FAILED",
                    f"Unexpected failure while creating the project: {exc}",
                    recoverable=True,
                )
                self._record_failure(connection, run.id, error)
                raise error from exc
            return project

    def list_projects(self) -> list[Project]:
        with self.database.connection() as connection:
            return ProjectRepository(connection).list()

    def get(self, project_id: str) -> Project:
        with self.database.connection() as connection:
            return ProjectRepository(connection).get(project_id)

    def runs(self, project_id: str) -> list[StageRun]:
        with self.database.connection() as connection:
            ProjectRepository(connection).get(project_id)
            return StageRunRepository(connection).list_for_project(project_id)

    def validate(self, project_id: str) -> list[ValidationIssue]:
        with self.database.connection() as connection:
            project = ProjectRepository(connection).get(project_id)
            return self._validate_with_connection(connection, project)

    def _materialize(
        self,
        connection: sqlite3.Connection,
        project: Project,
        run: StageRun,
        loaded: LoadedProjectConfig,
    ) -> None:
        self.store.initialize_project(project.artifact_root)
        stored_config = self.store.write_bytes(
            project.artifact_root, project.config_path, loaded.raw_bytes
        )
        stored_manifest = self.store.write_bytes(
            project.artifact_root, "project.json", project_manifest(project, loaded)
        )
        artifacts = ArtifactRepository(connection)
        runs = StageRunRepository(connection)
        with self.database.transaction(connection):
            for artifact_type, stored in (
                ("project_config", stored_config),
                ("project_manifest", stored_manifest),
            ):
                artifacts.add_idempotent(
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
            current = runs.get(run.id)
            done = self.state_machine.transition(current, RunStatus.DONE)
            runs.update(done, expected_status=current.status)

    def _prepare_retry(self, connection: sqlite3.Connection, run: StageRun) -> StageRun:
        runs = StageRunRepository(connection)
        current = run
        if current.status is RunStatus.FAILED:
            with self.database.transaction(connection):
                target = (
                    RunStatus.PENDING_RETRY
                    if current.attempt < current.max_attempts
                    else RunStatus.BLOCKED
                )
                updated = self.state_machine.transition(current, target)
                runs.update(updated, expected_status=current.status)
                current = updated
        if current.status is RunStatus.BLOCKED:
            raise ConflictError("PROJECT_BLOCKED", "Project creation is blocked")
        if current.status not in {RunStatus.PENDING, RunStatus.PENDING_RETRY}:
            raise ConflictError(
                "PROJECT_RUN_NOT_RETRYABLE", f"Cannot resume run in {current.status.value}"
            )
        with self.database.transaction(connection):
            running = self.state_machine.transition(current, RunStatus.RUNNING)
            runs.update(running, expected_status=current.status)
        return running

    def _record_failure(
        self, connection: sqlite3.Connection, run_id: str, error: HeliosError
    ) -> None:
        runs = StageRunRepository(connection)
        current = runs.get(run_id)
        if current.status is not RunStatus.RUNNING:
            return
        with self.database.transaction(connection):
            ErrorRepository(connection).add(
                project_id=current.project_id,
                stage_run_id=current.id,
                code=error.code,
                message=error.message,
                recoverable=error.recoverable,
                evidence=error.context.evidence,
            )
            failed = self.state_machine.transition(current, RunStatus.FAILED)
            runs.update(failed, expected_status=current.status)
            target = (
                RunStatus.PENDING_RETRY
                if error.recoverable and failed.attempt < failed.max_attempts
                else RunStatus.BLOCKED
            )
            final = self.state_machine.transition(failed, target)
            runs.update(final, expected_status=failed.status)

    def _validate_with_connection(
        self, connection: sqlite3.Connection, project: Project
    ) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        try:
            root = self.store.project_root(project.artifact_root)
        except HeliosError as exc:
            return [ValidationIssue(exc.code, exc.message)]
        if not root.is_dir():
            issues.append(ValidationIssue("PROJECT_ROOT_MISSING", "Project root is missing"))
        artifacts = ArtifactRepository(connection).list_for_project(project.id)
        for artifact in artifacts:
            try:
                inspected = self.store.inspect(project.artifact_root, artifact.relative_path)
            except HeliosError as exc:
                issues.append(ValidationIssue(exc.code, exc.message, artifact.relative_path))
                continue
            if inspected.sha256 != artifact.sha256 or inspected.byte_size != artifact.byte_size:
                issues.append(
                    ValidationIssue(
                        "ARTIFACT_HASH_MISMATCH",
                        f"Artifact {artifact.relative_path!r} differs from its database record",
                        artifact.relative_path,
                    )
                )
        done_runs = [
            run
            for run in StageRunRepository(connection).list_for_project(project.id)
            if run.status is RunStatus.DONE
        ]
        artifact_run_ids = {artifact.stage_run_id for artifact in artifacts}
        for run in done_runs:
            issue = self.done_run_evidence.validate(
                connection, run, has_artifact=run.id in artifact_run_ids
            )
            if issue is not None:
                issues.append(issue)
        issues.extend(self.runtime_config.validate_project(connection, project))
        return issues

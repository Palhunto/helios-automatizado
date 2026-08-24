from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from pydantic import Field, ValidationError

from ebook_pipeline.config import (
    AppConfig,
    ProjectRuntimeConfig,
    StrictModel,
    load_project_config,
)
from ebook_pipeline.core.errors import ConflictError, HeliosError, IntegrityError
from ebook_pipeline.core.hashing import canonical_hash, canonical_json_bytes, sha256_bytes
from ebook_pipeline.core.ids import new_id, utc_now
from ebook_pipeline.core.models import Artifact, Project, RunStatus, ValidationIssue
from ebook_pipeline.core.state_machine import StateMachine
from ebook_pipeline.storage.artifacts import ArtifactStore
from ebook_pipeline.storage.database import Database
from ebook_pipeline.storage.repositories import (
    ArtifactRepository,
    ProjectRepository,
    StageRunRepository,
)

RUNTIME_STAGE = "project_runtime_config"
RUNTIME_UNIT = "runtime:update"
RUNTIME_ARTIFACT_TYPE = "project_runtime_config"


class ProjectRuntimeSnapshot(StrictModel):
    project_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    source_config_path: str = Field(min_length=1)
    source_config_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    previous_snapshot_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    runtime: ProjectRuntimeConfig


@dataclass(frozen=True, slots=True)
class ResolvedProjectRuntime:
    project_id: str
    version: int
    source: str
    source_config_sha256: str
    snapshot_artifact_id: str | None
    snapshot_sha256: str | None
    model: ProjectRuntimeConfig


def _snapshot_path(version: int) -> str:
    return f"config/runtime/v{version:04d}.json"


def _runtime_input_hash(snapshot: ProjectRuntimeSnapshot) -> str:
    return canonical_hash(
        {
            "operation": "project.runtime.update",
            **snapshot.model_dump(mode="json"),
        }
    )


class ProjectRuntimeService:
    """Version project runtime settings without mutating the historical project YAML."""

    def __init__(self, config: AppConfig, database: Database, store: ArtifactStore) -> None:
        self.config = config
        self.database = database
        self.store = store
        self.state_machine = StateMachine()

    def resolve(self, project_id: str) -> ResolvedProjectRuntime:
        with self.database.connection() as connection:
            project = ProjectRepository(connection).get(project_id)
            return self._resolve_with_connection(connection, project)

    def set_browser_automation(
        self, project_id: str, enabled: bool
    ) -> ResolvedProjectRuntime:
        with self.database.connection() as connection:
            project = ProjectRepository(connection).get(project_id)
            current = self._resolve_with_connection(connection, project)
            if current.model.browser_automation_enabled is enabled:
                return current
            next_version = current.version + 1
            runtime = current.model.model_copy(
                update={"browser_automation_enabled": enabled}
            )
            snapshot = ProjectRuntimeSnapshot(
                project_id=project.id,
                version=next_version,
                source_config_path=project.config_path,
                source_config_sha256=current.source_config_sha256,
                previous_snapshot_sha256=current.snapshot_sha256,
                runtime=runtime,
            )
            content = canonical_json_bytes(snapshot.model_dump(mode="json")) + b"\n"
            stored = self.store.write_bytes(
                project.artifact_root, _snapshot_path(next_version), content
            )
            input_hash = _runtime_input_hash(snapshot)
            run = self.state_machine.new_run(
                project_id=project.id,
                stage_id=RUNTIME_STAGE,
                unit_id=RUNTIME_UNIT,
                input_hash=input_hash,
                max_attempts=self.config.max_attempts,
                version=next_version,
                supersedes_run_id=self._latest_runtime_run_id(connection, project.id),
            )
            with self.database.transaction(connection):
                latest = self._runtime_artifacts(connection, project.id)
                persisted_version = 0 if not latest else latest[-1].version
                if persisted_version != current.version:
                    raise ConflictError(
                        "PROJECT_RUNTIME_CONCURRENT_UPDATE",
                        "Project runtime configuration changed concurrently",
                    )
                runs = StageRunRepository(connection)
                runs.add(run)
                running = self.state_machine.transition(run, RunStatus.RUNNING)
                runs.update(running, expected_status=run.status)
                ArtifactRepository(connection).add_idempotent(
                    Artifact(
                        id=new_id(),
                        project_id=project.id,
                        stage_run_id=run.id,
                        artifact_type=RUNTIME_ARTIFACT_TYPE,
                        relative_path=stored.relative_path,
                        sha256=stored.sha256,
                        byte_size=stored.byte_size,
                        version=next_version,
                        created_at=utc_now(),
                    )
                )
                done = self.state_machine.transition(running, RunStatus.DONE)
                runs.update(done, expected_status=running.status)
            return self._resolve_with_connection(connection, project)

    def validate_project(
        self, connection: sqlite3.Connection, project: Project
    ) -> list[ValidationIssue]:
        try:
            self._resolve_with_connection(connection, project)
        except HeliosError as exc:
            return [ValidationIssue(exc.code, exc.message)]
        return []

    def _resolve_with_connection(
        self, connection: sqlite3.Connection, project: Project
    ) -> ResolvedProjectRuntime:
        loaded = load_project_config(
            self.store.resolve(project.artifact_root, project.config_path)
        )
        source_sha = sha256_bytes(loaded.raw_bytes)
        artifacts = self._runtime_artifacts(connection, project.id)
        if not artifacts:
            return ResolvedProjectRuntime(
                project_id=project.id,
                version=0,
                source="project_config",
                source_config_sha256=source_sha,
                snapshot_artifact_id=None,
                snapshot_sha256=None,
                model=loaded.model.runtime,
            )

        previous_sha: str | None = None
        latest_snapshot: ProjectRuntimeSnapshot | None = None
        for expected_version, artifact in enumerate(artifacts, start=1):
            if artifact.version != expected_version:
                raise IntegrityError(
                    "PROJECT_RUNTIME_VERSION_GAP",
                    "Project runtime snapshot versions are not contiguous",
                )
            if artifact.relative_path != _snapshot_path(expected_version):
                raise IntegrityError(
                    "PROJECT_RUNTIME_PATH_INVALID",
                    "Project runtime snapshot path differs from its version",
                )
            run = StageRunRepository(connection).get(artifact.stage_run_id)
            if (
                run.project_id != project.id
                or run.stage_id != RUNTIME_STAGE
                or run.unit_id != RUNTIME_UNIT
                or run.version != expected_version
                or run.status is not RunStatus.DONE
            ):
                raise IntegrityError(
                    "PROJECT_RUNTIME_RUN_INVALID",
                    "Project runtime snapshot is not bound to a completed runtime update",
                )
            inspected = self.store.inspect(project.artifact_root, artifact.relative_path)
            if inspected.sha256 != artifact.sha256 or inspected.byte_size != artifact.byte_size:
                raise IntegrityError(
                    "PROJECT_RUNTIME_ARTIFACT_INVALID",
                    "Project runtime snapshot differs from its artifact record",
                )
            try:
                value = json.loads(
                    self.store.resolve(project.artifact_root, artifact.relative_path).read_bytes()
                )
                snapshot = ProjectRuntimeSnapshot.model_validate(value)
            except (OSError, UnicodeError, json.JSONDecodeError, ValidationError) as exc:
                raise IntegrityError(
                    "PROJECT_RUNTIME_SNAPSHOT_INVALID",
                    f"Project runtime snapshot is invalid: {exc}",
                ) from exc
            if (
                snapshot.project_id != project.id
                or snapshot.version != expected_version
                or snapshot.source_config_path != project.config_path
                or snapshot.source_config_sha256 != source_sha
                or snapshot.previous_snapshot_sha256 != previous_sha
            ):
                raise IntegrityError(
                    "PROJECT_RUNTIME_PROVENANCE_INVALID",
                    "Project runtime snapshot provenance differs from its immutable chain",
                )
            if run.input_hash != _runtime_input_hash(snapshot):
                raise IntegrityError(
                    "PROJECT_RUNTIME_RUN_IDENTITY_INVALID",
                    "Project runtime run identity differs from its snapshot",
                )
            previous_sha = artifact.sha256
            latest_snapshot = snapshot

        assert latest_snapshot is not None
        latest_artifact = artifacts[-1]
        return ResolvedProjectRuntime(
            project_id=project.id,
            version=latest_snapshot.version,
            source="runtime_snapshot",
            source_config_sha256=source_sha,
            snapshot_artifact_id=latest_artifact.id,
            snapshot_sha256=latest_artifact.sha256,
            model=latest_snapshot.runtime,
        )

    @staticmethod
    def _runtime_artifacts(
        connection: sqlite3.Connection, project_id: str
    ) -> list[Artifact]:
        return sorted(
            (
                artifact
                for artifact in ArtifactRepository(connection).list_for_project(project_id)
                if artifact.artifact_type == RUNTIME_ARTIFACT_TYPE
            ),
            key=lambda artifact: artifact.version,
        )

    @staticmethod
    def _latest_runtime_run_id(
        connection: sqlite3.Connection, project_id: str
    ) -> str | None:
        run = StageRunRepository(connection).latest(project_id, RUNTIME_STAGE, RUNTIME_UNIT)
        return None if run is None else run.id

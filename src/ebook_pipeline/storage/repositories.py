from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from typing import Any

from ebook_pipeline.core.errors import ConflictError, NotFoundError
from ebook_pipeline.core.ids import new_id, utc_now
from ebook_pipeline.core.models import Artifact, ErrorRecord, Project, RunStatus, StageRun


def _project(row: sqlite3.Row) -> Project:
    return Project(**dict(row))


def _stage_run(row: sqlite3.Row) -> StageRun:
    values = dict(row)
    values["status"] = RunStatus(values["status"])
    return StageRun(**values)


def _artifact(row: sqlite3.Row) -> Artifact:
    return Artifact(**dict(row))


def _error_record(row: sqlite3.Row) -> ErrorRecord:
    values = dict(row)
    values["recoverable"] = bool(values["recoverable"])
    return ErrorRecord(**values)


class ProjectRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def add(self, project: Project) -> None:
        try:
            self.connection.execute(
                "INSERT INTO projects(id, name, slug, config_path, artifact_root, created_at, "
                "updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    project.id,
                    project.name,
                    project.slug,
                    project.config_path,
                    project.artifact_root,
                    project.created_at,
                    project.updated_at,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("PROJECT_CONFLICT", f"Could not create project: {exc}") from exc

    def get(self, project_id: str) -> Project:
        row = self.connection.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("PROJECT_NOT_FOUND", f"Project {project_id!r} was not found")
        return _project(row)

    def get_by_slug(self, slug: str) -> Project | None:
        row = self.connection.execute("SELECT * FROM projects WHERE slug = ?", (slug,)).fetchone()
        return None if row is None else _project(row)

    def list(self) -> list[Project]:
        rows = self.connection.execute("SELECT * FROM projects ORDER BY created_at, id").fetchall()
        return [_project(row) for row in rows]


class StageRunRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def add(self, run: StageRun) -> None:
        try:
            self.connection.execute(
                "INSERT INTO stage_runs(id, project_id, stage_id, unit_id, status, input_hash, "
                "idempotency_key, version, attempt, max_attempts, started_at, finished_at, "
                "created_at, updated_at, supersedes_run_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    run.id,
                    run.project_id,
                    run.stage_id,
                    run.unit_id,
                    run.status.value,
                    run.input_hash,
                    run.idempotency_key,
                    run.version,
                    run.attempt,
                    run.max_attempts,
                    run.started_at,
                    run.finished_at,
                    run.created_at,
                    run.updated_at,
                    run.supersedes_run_id,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("STAGE_RUN_CONFLICT", f"Could not create stage run: {exc}") from exc

    def get(self, run_id: str) -> StageRun:
        row = self.connection.execute("SELECT * FROM stage_runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise NotFoundError("STAGE_RUN_NOT_FOUND", f"Stage run {run_id!r} was not found")
        return _stage_run(row)

    def find_identity(
        self, project_id: str, stage_id: str, unit_id: str, input_hash: str, version: int = 1
    ) -> StageRun | None:
        row = self.connection.execute(
            "SELECT * FROM stage_runs WHERE project_id = ? AND stage_id = ? AND unit_id = ? "
            "AND input_hash = ? AND version = ?",
            (project_id, stage_id, unit_id, input_hash, version),
        ).fetchone()
        return None if row is None else _stage_run(row)

    def latest(self, project_id: str, stage_id: str, unit_id: str) -> StageRun | None:
        row = self.connection.execute(
            "SELECT * FROM stage_runs WHERE project_id = ? AND stage_id = ? AND unit_id = ? "
            "ORDER BY version DESC LIMIT 1",
            (project_id, stage_id, unit_id),
        ).fetchone()
        return None if row is None else _stage_run(row)

    def list_for_project(self, project_id: str) -> list[StageRun]:
        rows = self.connection.execute(
            "SELECT * FROM stage_runs WHERE project_id = ? ORDER BY created_at, id", (project_id,)
        ).fetchall()
        return [_stage_run(row) for row in rows]

    def list_running(self, project_id: str | None = None) -> list[StageRun]:
        if project_id is None:
            rows = self.connection.execute(
                "SELECT * FROM stage_runs WHERE status = 'running' ORDER BY created_at, id"
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT * FROM stage_runs WHERE project_id = ? AND status = 'running' "
                "ORDER BY created_at, id",
                (project_id,),
            ).fetchall()
        return [_stage_run(row) for row in rows]

    def update(self, run: StageRun, *, expected_status: RunStatus) -> None:
        cursor = self.connection.execute(
            "UPDATE stage_runs SET status = ?, attempt = ?, started_at = ?, finished_at = ?, "
            "updated_at = ? WHERE id = ? AND status = ?",
            (
                run.status.value,
                run.attempt,
                run.started_at,
                run.finished_at,
                run.updated_at,
                run.id,
                expected_status.value,
            ),
        )
        if cursor.rowcount != 1:
            raise ConflictError(
                "STAGE_RUN_CONCURRENT_UPDATE",
                f"Stage run {run.id!r} no longer has status {expected_status.value!r}",
            )


class ArtifactRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def add_idempotent(self, artifact: Artifact) -> Artifact:
        existing = self.get_by_path(artifact.project_id, artifact.relative_path)
        if existing is not None:
            if (
                existing.sha256 == artifact.sha256
                and existing.byte_size == artifact.byte_size
                and existing.stage_run_id == artifact.stage_run_id
                and existing.artifact_type == artifact.artifact_type
                and existing.version == artifact.version
            ):
                return existing
            raise ConflictError(
                "ARTIFACT_RECORD_CONFLICT",
                f"Artifact path {artifact.relative_path!r} is already registered differently",
            )
        try:
            self.connection.execute(
                "INSERT INTO artifacts(id, project_id, stage_run_id, artifact_type, relative_path, "
                "sha256, byte_size, version, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    artifact.id,
                    artifact.project_id,
                    artifact.stage_run_id,
                    artifact.artifact_type,
                    artifact.relative_path,
                    artifact.sha256,
                    artifact.byte_size,
                    artifact.version,
                    artifact.created_at,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("ARTIFACT_RECORD_CONFLICT", str(exc)) from exc
        return artifact

    def get_by_path(self, project_id: str, relative_path: str) -> Artifact | None:
        row = self.connection.execute(
            "SELECT * FROM artifacts WHERE project_id = ? AND relative_path = ?",
            (project_id, relative_path),
        ).fetchone()
        return None if row is None else _artifact(row)

    def list_for_project(self, project_id: str) -> list[Artifact]:
        rows = self.connection.execute(
            "SELECT * FROM artifacts WHERE project_id = ? ORDER BY created_at, id", (project_id,)
        ).fetchall()
        return [_artifact(row) for row in rows]


class ErrorRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def add(
        self,
        *,
        project_id: str,
        stage_run_id: str,
        code: str,
        message: str,
        recoverable: bool,
        evidence: dict[str, Any] | None = None,
    ) -> ErrorRecord:
        record = ErrorRecord(
            id=new_id(),
            project_id=project_id,
            stage_run_id=stage_run_id,
            code=code,
            message=message,
            recoverable=recoverable,
            evidence_json=(
                json.dumps(evidence, ensure_ascii=False, sort_keys=True)
                if evidence is not None
                else None
            ),
            created_at=utc_now(),
        )
        self.connection.execute(
            "INSERT INTO error_records(id, project_id, stage_run_id, code, message, recoverable, "
            "evidence_json, created_at, resolved_at, resolution) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.id,
                record.project_id,
                record.stage_run_id,
                record.code,
                record.message,
                int(record.recoverable),
                record.evidence_json,
                record.created_at,
                record.resolved_at,
                record.resolution,
            ),
        )
        return record

    def list_for_project(self, project_id: str) -> list[ErrorRecord]:
        rows = self.connection.execute(
            "SELECT * FROM error_records WHERE project_id = ? ORDER BY created_at, id",
            (project_id,),
        ).fetchall()
        return [_error_record(row) for row in rows]

    def latest_for_run(self, stage_run_id: str) -> ErrorRecord | None:
        row = self.connection.execute(
            "SELECT * FROM error_records WHERE stage_run_id = ? ORDER BY created_at DESC, "
            "id DESC LIMIT 1",
            (stage_run_id,),
        ).fetchone()
        return None if row is None else _error_record(row)


def touch_project(project: Project) -> Project:
    return replace(project, updated_at=utc_now())

from __future__ import annotations

import logging
import sqlite3
from typing import TYPE_CHECKING

from ebook_pipeline.core.errors import HeliosError, IntegrityError
from ebook_pipeline.core.models import Project, RunStatus, StageRun, StoredFile
from ebook_pipeline.storage.repositories import (
    ErrorRepository,
    ProjectRepository,
    StageRunRepository,
)

if TYPE_CHECKING:
    from ebook_pipeline.visual_planning.service import VisualPlanningService


def resume(visual: VisualPlanningService, connection: sqlite3.Connection, run_id: str) -> StageRun:
    runs = StageRunRepository(connection)
    run = runs.get(run_id)
    if run.status is RunStatus.PENDING_RETRY:
        running = visual.state_machine.transition(run, RunStatus.RUNNING)
        runs.update(running, expected_status=run.status)
        return running
    if run.status not in {RunStatus.RUNNING, RunStatus.DONE}:
        raise IntegrityError("VISUAL_RECOVERY_REQUIRED", "Visual operation cannot resume")
    return run


def record_failure(visual: VisualPlanningService, run_id: str, exc: Exception) -> None:
    with visual.database.connection() as connection, visual.database.transaction(connection):
        runs = StageRunRepository(connection)
        run = runs.get(run_id)
        if run.status is not RunStatus.RUNNING:
            return
        code = exc.code if isinstance(exc, HeliosError) else "VISUAL_OPERATION_FAILED"
        ErrorRepository(connection).add(
            project_id=run.project_id,
            stage_run_id=run.id,
            code=code,
            message=exc.message if isinstance(exc, HeliosError) else type(exc).__name__,
            recoverable=run.attempt < run.max_attempts,
        )
        retry = visual.state_machine.transition(run, RunStatus.PENDING_RETRY, recovery=True)
        runs.update(retry, expected_status=run.status)
    log_result(run, "failed")


def log_result(run: StageRun, result: str) -> None:
    logging.getLogger(__name__).info(
        "Visual operation %s",
        result,
        extra={
            "project_id": run.project_id,
            "stage_id": run.stage_id,
            "unit_id": run.unit_id,
            "action": "visual_operation",
            "result": result,
        },
    )


def read_owned(
    visual: VisualPlanningService,
    connection: sqlite3.Connection,
    run: StageRun,
    artifact_id: str | None,
    sha256: str | None,
    kind: str,
) -> bytes:
    if artifact_id is None or sha256 is None:
        raise IntegrityError("VISUAL_ARTIFACT_MISSING", "Visual artifact evidence is incomplete")
    row = connection.execute(
        "SELECT stage_run_id, artifact_type FROM artifacts WHERE id = ?", (artifact_id,)
    ).fetchone()
    if row is None or tuple(row) != (run.id, kind):
        raise IntegrityError("VISUAL_ARTIFACT_OWNER_INVALID", "Artifact producer or type differs")
    return visual.persistence.read_artifact(
        connection,
        ProjectRepository(connection).get(run.project_id),
        artifact_id,
        expected_sha256=sha256,
    )


def verify_files(visual: VisualPlanningService, project: Project, *files: StoredFile) -> None:
    """Recheck published bytes at the DB commit boundary, including crash recovery."""
    for stored in files:
        actual = visual.store.inspect(project.artifact_root, stored.relative_path)
        if actual.sha256 != stored.sha256 or actual.byte_size != stored.byte_size:
            raise IntegrityError("VISUAL_OUTPUT_CHANGED", "Visual output changed before commit")

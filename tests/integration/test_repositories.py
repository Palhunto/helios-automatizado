from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from conftest import ServiceBundle

from ebook_pipeline.core.errors import ConflictError, NotFoundError
from ebook_pipeline.core.hashing import idempotency_key, sha256_bytes
from ebook_pipeline.core.ids import new_id, utc_now
from ebook_pipeline.core.models import Artifact, RunStatus
from ebook_pipeline.storage.repositories import (
    ArtifactRepository,
    ErrorRepository,
    ProjectRepository,
    StageRunRepository,
    touch_project,
)


def _artifact_count(services: ServiceBundle, project_id: str) -> int:
    with services.database.connection() as connection:
        row = connection.execute(
            "SELECT count(*) FROM artifacts WHERE project_id = ?", (project_id,)
        ).fetchone()
    assert row is not None
    return int(row[0])


def test_duplicate_project_constraint_is_translated(
    services: ServiceBundle, project_config_path: Path
) -> None:
    project = services.projects.create(project_config_path)
    with services.database.connection() as connection, pytest.raises(ConflictError) as captured:
        ProjectRepository(connection).add(project)
    assert captured.value.code == "PROJECT_CONFLICT"
    assert services.projects.list_projects() == [project]


def test_duplicate_and_invalid_stage_run_constraints_are_translated(
    services: ServiceBundle, project_config_path: Path
) -> None:
    project = services.projects.create(project_config_path)
    existing = services.projects.runs(project.id)[0]
    with services.database.connection() as connection:
        repository = StageRunRepository(connection)
        with pytest.raises(ConflictError) as duplicate:
            repository.add(existing)
        assert duplicate.value.code == "STAGE_RUN_CONFLICT"

        digest = sha256_bytes(b"invalid-fk")
        invalid = replace(
            existing,
            id=new_id(),
            project_id=new_id(),
            input_hash=digest,
            idempotency_key=idempotency_key(
                project_id="missing-project",
                stage_id="other",
                unit_id="other",
                input_hash=digest,
                version=1,
            ),
            stage_id="other",
            unit_id="other",
            status=RunStatus.PENDING,
            attempt=0,
            started_at=None,
            finished_at=None,
        )
        with pytest.raises(ConflictError) as foreign_key:
            repository.add(invalid)
        assert foreign_key.value.code == "STAGE_RUN_CONFLICT"


def test_artifact_invalid_fk_and_hash_constraints_are_translated(
    services: ServiceBundle, project_config_path: Path
) -> None:
    project = services.projects.create(project_config_path)
    run = services.projects.runs(project.id)[0]
    base = Artifact(
        id=new_id(),
        project_id=project.id,
        stage_run_id=new_id(),
        artifact_type="invalid_fk",
        relative_path="invalid/fk.txt",
        sha256=sha256_bytes(b"value"),
        byte_size=5,
        version=1,
        created_at=utc_now(),
    )
    with services.database.connection() as connection:
        repository = ArtifactRepository(connection)
        with pytest.raises(ConflictError):
            repository.add_idempotent(base)
        invalid_hash = replace(
            base,
            id=new_id(),
            stage_run_id=run.id,
            artifact_type="invalid_hash",
            relative_path="invalid/hash.txt",
            sha256="not-a-hash",
        )
        with pytest.raises(ConflictError):
            repository.add_idempotent(invalid_hash)


def test_compare_and_set_rejects_stale_status_without_overwrite(
    services: ServiceBundle, project_config_path: Path
) -> None:
    project = services.projects.create(project_config_path)
    stale = services.projects.runs(project.id)[0]
    with services.database.connection() as connection:
        connection.execute("UPDATE stage_runs SET status = 'blocked' WHERE id = ?", (stale.id,))
        candidate = replace(stale, status=RunStatus.SKIPPED)
        with pytest.raises(ConflictError) as captured:
            StageRunRepository(connection).update(candidate, expected_status=RunStatus.DONE)
        persisted = StageRunRepository(connection).get(stale.id)
    assert captured.value.code == "STAGE_RUN_CONCURRENT_UPDATE"
    assert persisted.status is RunStatus.BLOCKED


def test_artifact_repository_is_idempotent_for_exact_record(
    services: ServiceBundle, project_config_path: Path
) -> None:
    project = services.projects.create(project_config_path)
    with services.database.connection() as connection:
        repository = ArtifactRepository(connection)
        artifact = repository.list_for_project(project.id)[0]
        returned = repository.add_idempotent(artifact)
    assert returned == artifact
    assert _artifact_count(services, project.id) == 2


@pytest.mark.parametrize(
    "change",
    [
        {"sha256": "0" * 64},
        {"artifact_type": "different_type"},
        {"byte_size": 999999},
    ],
    ids=["hash", "type", "size"],
)
def test_artifact_repository_rejects_conflicting_metadata(
    services: ServiceBundle, project_config_path: Path, change: dict[str, object]
) -> None:
    project = services.projects.create(project_config_path)
    with services.database.connection() as connection:
        repository = ArtifactRepository(connection)
        original = repository.list_for_project(project.id)[0]
        if "sha256" in change:
            conflicting = replace(original, id=new_id(), sha256=str(change["sha256"]))
        elif "artifact_type" in change:
            conflicting = replace(
                original,
                id=new_id(),
                artifact_type=str(change["artifact_type"]),
            )
        else:
            byte_size = change["byte_size"]
            assert isinstance(byte_size, int)
            conflicting = replace(original, id=new_id(), byte_size=byte_size)
        with pytest.raises(ConflictError, match="already registered differently"):
            repository.add_idempotent(conflicting)
        preserved = repository.get_by_path(project.id, original.relative_path)
    assert preserved == original
    assert _artifact_count(services, project.id) == 2


def test_stage_run_queries_cover_missing_latest_and_global_running(
    services: ServiceBundle, project_config_path: Path
) -> None:
    project = services.projects.create(project_config_path)
    run = services.projects.runs(project.id)[0]
    with services.database.connection() as connection:
        repository = StageRunRepository(connection)
        with pytest.raises(NotFoundError):
            repository.get("missing")
        assert repository.latest(project.id, run.stage_id, run.unit_id) == run
        assert repository.latest(project.id, "missing", "missing") is None
        connection.execute(
            "UPDATE stage_runs SET status = 'running', finished_at = NULL WHERE id = ?", (run.id,)
        )
        assert [item.id for item in repository.list_running()] == [run.id]


def test_error_records_are_read_back_and_touch_project_changes_only_timestamp(
    services: ServiceBundle, project_config_path: Path
) -> None:
    project = services.projects.create(project_config_path)
    run = services.projects.runs(project.id)[0]
    with services.database.connection() as connection:
        errors = ErrorRepository(connection)
        added = errors.add(
            project_id=project.id,
            stage_run_id=run.id,
            code="TEST_ERROR",
            message="controlled",
            recoverable=True,
            evidence={"kind": "test"},
        )
        assert errors.list_for_project(project.id) == [added]
    touched = touch_project(project)
    assert touched.updated_at != project.updated_at
    assert replace(touched, updated_at=project.updated_at) == project

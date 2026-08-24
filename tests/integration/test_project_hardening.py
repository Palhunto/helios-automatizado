from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import ServiceBundle

from ebook_pipeline.core.errors import ArtifactError, ConflictError, IntegrityError
from ebook_pipeline.core.models import Project, RunStatus, ValidationIssue
from ebook_pipeline.core.state_machine import StateMachine
from ebook_pipeline.storage.repositories import ErrorRepository, StageRunRepository

ProjectMutation = Callable[[ServiceBundle, Project], None]


def _missing_root(services: ServiceBundle, project: Project) -> None:
    shutil.rmtree(services.store.project_root(project.artifact_root))


def _invalid_root(services: ServiceBundle, project: Project) -> None:
    with services.database.connection() as connection:
        connection.execute(
            "UPDATE projects SET artifact_root = '../escape' WHERE id = ?", (project.id,)
        )


def _missing_file(services: ServiceBundle, project: Project) -> None:
    services.store.resolve(project.artifact_root, project.config_path).unlink()


def _wrong_hash(services: ServiceBundle, project: Project) -> None:
    with services.database.connection() as connection:
        connection.execute(
            "UPDATE artifacts SET sha256 = ? WHERE project_id = ? AND artifact_type = ?",
            ("0" * 64, project.id, "project_config"),
        )


def _wrong_size(services: ServiceBundle, project: Project) -> None:
    with services.database.connection() as connection:
        connection.execute(
            "UPDATE artifacts SET byte_size = byte_size + 1 "
            "WHERE project_id = ? AND artifact_type = ?",
            (project.id, "project_config"),
        )


def _done_without_artifacts(services: ServiceBundle, project: Project) -> None:
    with services.database.connection() as connection:
        connection.execute("DELETE FROM artifacts WHERE project_id = ?", (project.id,))


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        (_invalid_root, "PATH_INVALID"),
        (_missing_root, "PROJECT_ROOT_MISSING"),
        (_missing_file, "ARTIFACT_MISSING"),
        (_wrong_hash, "ARTIFACT_HASH_MISMATCH"),
        (_wrong_size, "ARTIFACT_HASH_MISMATCH"),
        (_done_without_artifacts, "DONE_RUN_WITHOUT_ARTIFACT"),
    ],
    ids=["invalid-root", "missing-root", "missing-file", "hash", "size", "no-artifacts"],
)
def test_validate_reports_each_db_filesystem_inconsistency(
    services: ServiceBundle,
    project_config_path: Path,
    mutation: ProjectMutation,
    expected_code: str,
) -> None:
    project = services.projects.create(project_config_path)
    mutation(services, project)
    issues = services.projects.validate(project.id)
    assert expected_code in {issue.code for issue in issues}


@pytest.mark.parametrize(
    "mutation",
    [_missing_root, _missing_file, _wrong_hash, _wrong_size, _done_without_artifacts],
    ids=["missing-root", "missing-file", "hash", "size", "no-artifacts"],
)
def test_existing_done_project_is_not_accepted_when_inconsistent(
    services: ServiceBundle,
    project_config_path: Path,
    mutation: ProjectMutation,
) -> None:
    project = services.projects.create(project_config_path)
    mutation(services, project)
    with pytest.raises(IntegrityError, match="integrity issue"):
        services.projects.create(project_config_path)
    assert services.projects.runs(project.id)[0].status is RunStatus.DONE


def test_existing_running_creation_requires_explicit_recovery(
    services: ServiceBundle, project_config_path: Path
) -> None:
    project = services.projects.create(project_config_path)
    with services.database.connection() as connection:
        connection.execute(
            "UPDATE stage_runs SET status = 'running', finished_at = NULL WHERE project_id = ?",
            (project.id,),
        )
    with pytest.raises(IntegrityError, match="run project recover first"):
        services.projects.create(project_config_path)
    assert services.projects.runs(project.id)[0].status is RunStatus.RUNNING


def _errors(services: ServiceBundle, project_id: str):  # type: ignore[no-untyped-def]
    with services.database.connection() as connection:
        return ErrorRepository(connection).list_for_project(project_id)


def test_recoverable_domain_failure_persists_error_and_can_retry(
    services: ServiceBundle,
    project_config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = services.store.write_bytes
    calls = 0

    def fail_after_valid_config(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls == 2:
            raise ArtifactError("INJECTED_DOMAIN_FAILURE", "promotion failed", recoverable=True)
        return original(*args, **kwargs)

    monkeypatch.setattr(services.store, "write_bytes", fail_after_valid_config)
    with pytest.raises(ArtifactError, match="promotion failed"):
        services.projects.create(project_config_path)

    project = services.projects.list_projects()[0]
    run = services.projects.runs(project.id)[0]
    assert run.status is RunStatus.PENDING_RETRY
    assert services.store.resolve(project.artifact_root, project.config_path).read_bytes() == (
        project_config_path.read_bytes()
    )
    records = _errors(services, project.id)
    assert [(record.code, record.recoverable) for record in records] == [
        ("INJECTED_DOMAIN_FAILURE", True)
    ]

    monkeypatch.setattr(services.store, "write_bytes", original)
    retried = services.projects.create(project_config_path)
    assert retried.id == project.id
    assert services.projects.runs(project.id)[0].status is RunStatus.DONE
    assert services.projects.validate(project.id) == []


def test_unexpected_filesystem_failure_is_typed_and_does_not_leave_running(
    services: ServiceBundle,
    project_config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_initialize(_: str) -> Path:
        raise OSError("disk unavailable")

    monkeypatch.setattr(services.store, "initialize_project", fail_initialize)
    with pytest.raises(IntegrityError) as captured:
        services.projects.create(project_config_path)
    assert captured.value.code == "PROJECT_CREATE_FAILED"

    project = services.projects.list_projects()[0]
    assert services.projects.runs(project.id)[0].status is RunStatus.PENDING_RETRY
    records = _errors(services, project.id)
    assert [(record.code, record.recoverable) for record in records] == [
        ("PROJECT_CREATE_FAILED", True)
    ]


def test_nonrecoverable_domain_failure_blocks_creation(
    services: ServiceBundle,
    project_config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def conflict(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise ConflictError("INJECTED_CONFLICT", "destination occupied")

    monkeypatch.setattr(services.store, "write_bytes", conflict)
    with pytest.raises(ConflictError, match="destination occupied"):
        services.projects.create(project_config_path)
    project = services.projects.list_projects()[0]
    assert services.projects.runs(project.id)[0].status is RunStatus.BLOCKED
    assert _errors(services, project.id)[0].code == "INJECTED_CONFLICT"


def test_artifact_registration_failure_rolls_back_metadata_and_preserves_files(
    services: ServiceBundle,
    project_config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ebook_pipeline.storage import repositories

    def fail_registration(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise ArtifactError(
            "INJECTED_REGISTRATION_FAILURE",
            "database write failed",
            recoverable=True,
        )

    monkeypatch.setattr(repositories.ArtifactRepository, "add_idempotent", fail_registration)
    with pytest.raises(ArtifactError, match="database write failed"):
        services.projects.create(project_config_path)

    project = services.projects.list_projects()[0]
    assert services.store.resolve(project.artifact_root, project.config_path).is_file()
    assert services.store.resolve(project.artifact_root, "project.json").is_file()
    with services.database.connection() as connection:
        count = connection.execute(
            "SELECT count(*) FROM artifacts WHERE project_id = ?", (project.id,)
        ).fetchone()[0]
    assert count == 0
    assert services.projects.runs(project.id)[0].status is RunStatus.PENDING_RETRY


def test_record_failure_is_noop_when_run_is_no_longer_running(
    services: ServiceBundle, project_config_path: Path
) -> None:
    project = services.projects.create(project_config_path)
    run = services.projects.runs(project.id)[0]
    with services.database.connection() as connection:
        services.projects._record_failure(  # noqa: SLF001
            connection,
            run.id,
            ArtifactError("LATE_ERROR", "late failure", recoverable=True),
        )
    assert _errors(services, project.id) == []


def test_validate_returns_structured_issue_for_invalid_root(
    services: ServiceBundle, project_config_path: Path
) -> None:
    project = services.projects.create(project_config_path)
    _invalid_root(services, project)
    issues = services.projects.validate(project.id)
    assert issues == [
        ValidationIssue(code="PATH_INVALID", message="Unsafe relative path: '../escape'")
    ]


def test_done_operation_without_declared_database_evidence_still_requires_artifact(
    services: ServiceBundle, project_config_path: Path
) -> None:
    project = services.projects.create(project_config_path)
    states = StateMachine()
    pending = states.new_run(
        project_id=project.id,
        stage_id="artifact_producing_stage",
        unit_id="artifact_producing_unit",
        input_hash="a" * 64,
        max_attempts=1,
    )
    running = states.transition(pending, RunStatus.RUNNING)
    done = states.transition(running, RunStatus.DONE)
    with services.database.connection() as connection:
        repository = StageRunRepository(connection)
        with services.database.transaction(connection):
            repository.add(pending)
            repository.update(running, expected_status=pending.status)
            repository.update(done, expected_status=running.status)

    issues = services.projects.validate(project.id)
    assert [issue.code for issue in issues] == ["DONE_RUN_WITHOUT_ARTIFACT"]

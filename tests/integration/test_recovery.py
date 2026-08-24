from pathlib import Path

from conftest import ServiceBundle

from ebook_pipeline.core.models import RunStatus


def _orphan_run(services: ServiceBundle, project_id: str) -> None:
    with services.database.connection() as connection:
        connection.execute(
            "UPDATE stage_runs SET status = 'running', finished_at = NULL WHERE project_id = ?",
            (project_id,),
        )
        connection.execute("DELETE FROM artifacts WHERE project_id = ?", (project_id,))


def test_recovery_reconciles_complete_artifacts(
    services: ServiceBundle, project_config_path: Path
) -> None:
    project = services.projects.create(project_config_path)
    _orphan_run(services, project.id)
    result = services.recovery.recover(project.id)
    assert [item.status for item in result] == [RunStatus.DONE]
    assert services.projects.validate(project.id) == []
    assert services.recovery.recover(project.id) == []


def test_recovery_marks_missing_artifact_pending_retry(
    services: ServiceBundle, project_config_path: Path
) -> None:
    project = services.projects.create(project_config_path)
    _orphan_run(services, project.id)
    services.store.resolve(project.artifact_root, "project.json").unlink()
    result = services.recovery.recover(project.id)
    assert [item.status for item in result] == [RunStatus.PENDING_RETRY]


def test_recovery_blocks_corrupt_final_artifact(
    services: ServiceBundle, project_config_path: Path
) -> None:
    project = services.projects.create(project_config_path)
    _orphan_run(services, project.id)
    services.store.resolve(project.artifact_root, "project.json").write_text(
        "{}\n", encoding="utf-8"
    )
    result = services.recovery.recover(project.id)
    assert [item.status for item in result] == [RunStatus.BLOCKED]


def test_recovery_blocks_non_object_manifest_idempotently(
    services: ServiceBundle, project_config_path: Path
) -> None:
    project = services.projects.create(project_config_path)
    _orphan_run(services, project.id)
    services.store.resolve(project.artifact_root, "project.json").write_text(
        "[]\n", encoding="utf-8"
    )
    result = services.recovery.recover(project.id)
    assert [item.status for item in result] == [RunStatus.BLOCKED]
    assert services.recovery.recover(project.id) == []

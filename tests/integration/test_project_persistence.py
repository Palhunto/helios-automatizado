from pathlib import Path

import pytest
from conftest import ServiceBundle

from ebook_pipeline.core.errors import ConflictError
from ebook_pipeline.core.models import RunStatus
from ebook_pipeline.core.projects import ProjectService


def test_create_is_idempotent_and_survives_restart(
    services: ServiceBundle, project_config_path: Path
) -> None:
    first = services.projects.create(project_config_path)
    second = services.projects.create(project_config_path)
    assert first == second
    assert services.projects.validate(first.id) == []
    runs = services.projects.runs(first.id)
    assert len(runs) == 1
    assert runs[0].status is RunStatus.DONE

    restarted = ProjectService(services.config, services.database, services.store)
    assert restarted.get(first.id) == first
    assert restarted.list_projects() == [first]


def test_same_slug_with_changed_input_conflicts(
    services: ServiceBundle, project_config_path: Path, tmp_path: Path
) -> None:
    services.projects.create(project_config_path)
    changed = tmp_path / "changed.yaml"
    changed.write_text(
        project_config_path.read_text(encoding="utf-8").replace(
            'discipline: "Disciplina de exemplo"', 'discipline: "Outra disciplina"'
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConflictError, match="different project inputs"):
        services.projects.create(changed)


def test_exhausted_failed_run_is_persistently_blocked(
    services: ServiceBundle, project_config_path: Path
) -> None:
    project = services.projects.create(project_config_path)
    with services.database.connection() as connection:
        connection.execute(
            "UPDATE stage_runs SET status = 'failed', attempt = max_attempts, "
            "finished_at = updated_at WHERE project_id = ?",
            (project.id,),
        )
    with pytest.raises(ConflictError, match="blocked"):
        services.projects.create(project_config_path)
    assert services.projects.runs(project.id)[0].status is RunStatus.BLOCKED

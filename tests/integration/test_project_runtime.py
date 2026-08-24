from __future__ import annotations

from pathlib import Path

import pytest
from browser_fakes import FakeChatAdapter
from conftest import ServiceBundle

from ebook_pipeline.browser.service import BrowserAutomationService
from ebook_pipeline.core.errors import ConflictError
from ebook_pipeline.core.runtime_config import ProjectRuntimeService


def test_runtime_override_is_versioned_idempotent_and_preserves_historical_config(
    services: ServiceBundle, project_config_path: Path
) -> None:
    project = services.projects.create(project_config_path)
    historical_config = services.store.resolve(project.artifact_root, project.config_path)
    original_bytes = historical_config.read_bytes()

    initial = services.projects.runtime_config.resolve(project.id)
    assert initial.version == 0
    assert initial.source == "project_config"
    assert initial.model.browser_automation_enabled is False

    enabled = services.projects.runtime_config.set_browser_automation(project.id, True)
    assert enabled.version == 1
    assert enabled.source == "runtime_snapshot"
    assert enabled.model.browser_automation_enabled is True
    assert enabled.snapshot_artifact_id is not None
    assert historical_config.read_bytes() == original_bytes

    with services.database.connection() as connection:
        first_counts = (
            connection.execute(
                "SELECT count(*) FROM stage_runs WHERE project_id = ? "
                "AND stage_id = 'project_runtime_config'",
                (project.id,),
            ).fetchone()[0],
            connection.execute(
                "SELECT count(*) FROM artifacts WHERE project_id = ? "
                "AND artifact_type = 'project_runtime_config'",
                (project.id,),
            ).fetchone()[0],
        )
    repeated = services.projects.runtime_config.set_browser_automation(project.id, True)
    assert repeated == enabled
    with services.database.connection() as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM stage_runs WHERE project_id = ? "
                "AND stage_id = 'project_runtime_config'",
                (project.id,),
            ).fetchone()[0],
            connection.execute(
                "SELECT count(*) FROM artifacts WHERE project_id = ? "
                "AND artifact_type = 'project_runtime_config'",
                (project.id,),
            ).fetchone()[0],
        ) == first_counts

    restarted = ProjectRuntimeService(services.config, services.database, services.store)
    assert restarted.resolve(project.id) == enabled
    disabled = restarted.set_browser_automation(project.id, False)
    assert disabled.version == 2
    assert disabled.model.browser_automation_enabled is False
    assert services.store.resolve(
        project.artifact_root, "config/runtime/v0001.json"
    ).is_file()
    assert services.store.resolve(
        project.artifact_root, "config/runtime/v0002.json"
    ).is_file()
    assert historical_config.read_bytes() == original_bytes
    assert services.projects.validate(project.id) == []


def test_browser_gate_uses_effective_versioned_runtime(
    services: ServiceBundle, project_config_path: Path
) -> None:
    project = services.projects.create(project_config_path)
    browser = BrowserAutomationService(
        services.config,
        services.database,
        services.store,
        FakeChatAdapter(),
        services.writing,
    )
    with pytest.raises(ConflictError) as disabled:
        browser._assert_browser_enabled(project.id)  # noqa: SLF001
    assert disabled.value.code == "BROWSER_AUTOMATION_DISABLED"

    services.projects.runtime_config.set_browser_automation(project.id, True)
    browser._assert_browser_enabled(project.id)  # noqa: SLF001

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from ebook_pipeline.academic.service import AcademicService
from ebook_pipeline.config import AppConfig
from ebook_pipeline.core.projects import ProjectService
from ebook_pipeline.core.recovery import RecoveryService
from ebook_pipeline.storage.artifacts import ArtifactStore
from ebook_pipeline.storage.database import Database
from ebook_pipeline.writing.service import WritingService


@dataclass(frozen=True, slots=True)
class ServiceBundle:
    config: AppConfig
    database: Database
    store: ArtifactStore
    projects: ProjectService
    recovery: RecoveryService
    academic: AcademicService
    writing: WritingService


@pytest.fixture
def repository_root() -> Path:
    return Path(__file__).resolve().parents[1]


@pytest.fixture
def project_config_path(repository_root: Path) -> Path:
    return repository_root / "config" / "project.example.yaml"


@pytest.fixture
def services(tmp_path: Path, repository_root: Path) -> ServiceBundle:
    config = AppConfig(
        data_dir=tmp_path / "data",
        projects_dir=tmp_path / "projects",
        pipeline_config=repository_root / "config" / "pipeline.yaml",
        prompt_registry=repository_root / "prompts" / "registry.yaml",
        writing_contract_registry=repository_root / "writing_contracts" / "registry.yaml",
        log_level="INFO",
        max_attempts=3,
    )
    database = Database(config.database_path)
    store = ArtifactStore(config.projects_dir)
    academic = AcademicService(config, database, store)
    return ServiceBundle(
        config=config,
        database=database,
        store=store,
        projects=ProjectService(config, database, store),
        recovery=RecoveryService(config, database, store),
        academic=academic,
        writing=WritingService(config, database, store, academic),
    )

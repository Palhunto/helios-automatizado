from __future__ import annotations

import sqlite3

from ebook_pipeline.academic.service import AcademicService
from ebook_pipeline.config import AppConfig
from ebook_pipeline.core.models import Project, RecoveryResult, StageRun
from ebook_pipeline.storage.artifacts import ArtifactStore
from ebook_pipeline.storage.database import Database


class AcademicRecovery:
    """Dedicated recovery boundary for interrupted M1 academic operations."""

    def __init__(self, config: AppConfig, database: Database, store: ArtifactStore) -> None:
        self._service = AcademicService(config, database, store)

    def recover_run(
        self, connection: sqlite3.Connection, project: Project, run: StageRun
    ) -> RecoveryResult | None:
        return self._service.recover_run(connection, project, run)

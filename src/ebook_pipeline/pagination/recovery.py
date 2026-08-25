from __future__ import annotations

import sqlite3

from ebook_pipeline.core.errors import IntegrityError
from ebook_pipeline.core.models import RunStatus, StageRun
from ebook_pipeline.core.state_machine import StateMachine
from ebook_pipeline.pagination.models import VisualPaginationSnapshot
from ebook_pipeline.pagination.repositories import PaginationRepository
from ebook_pipeline.storage.repositories import StageRunRepository


class PaginationRecovery:
    def __init__(self) -> None:
        self.state_machine = StateMachine()

    def resume(
        self,
        connection: sqlite3.Connection,
        snapshot: VisualPaginationSnapshot,
    ) -> StageRun:
        run_repository = StageRunRepository(connection)
        run = run_repository.get(snapshot.stage_run_id)
        if run.status is RunStatus.DONE:
            return run
        if snapshot.html_artifact_id is not None or snapshot.pdf_artifact_id is not None:
            raise IntegrityError(
                "PAGINATION_RECOVERY_EVIDENCE_INCOMPLETE",
                "Incomplete pagination run already points to final artifacts",
            )
        pages = PaginationRepository(connection).page_rows(snapshot.id)
        if pages:
            raise IntegrityError(
                "PAGINATION_RECOVERY_PAGE_LEDGER_INCOMPLETE",
                "Incomplete pagination run already contains page ledger rows",
            )
        if run.status is RunStatus.RUNNING:
            return run
        if run.status is RunStatus.PENDING_RETRY:
            running = self.state_machine.transition(run, RunStatus.RUNNING)
            run_repository.update(running, expected_status=run.status)
            return running
        raise IntegrityError(
            "PAGINATION_RECOVERY_REQUIRED",
            f"Pagination run in state {run.status.value!r} cannot resume automatically",
        )

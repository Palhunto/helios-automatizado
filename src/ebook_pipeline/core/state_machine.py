from __future__ import annotations

from dataclasses import replace

from ebook_pipeline.core.errors import StateTransitionError
from ebook_pipeline.core.hashing import idempotency_key
from ebook_pipeline.core.ids import new_id, utc_now
from ebook_pipeline.core.models import RunStatus, StageRun

ALLOWED_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.PENDING: frozenset({RunStatus.RUNNING, RunStatus.BLOCKED, RunStatus.SKIPPED}),
    RunStatus.RUNNING: frozenset({RunStatus.DONE, RunStatus.FAILED, RunStatus.PENDING_RETRY}),
    RunStatus.FAILED: frozenset({RunStatus.PENDING_RETRY, RunStatus.BLOCKED}),
    RunStatus.PENDING_RETRY: frozenset({RunStatus.RUNNING, RunStatus.BLOCKED}),
    RunStatus.BLOCKED: frozenset({RunStatus.PENDING, RunStatus.PENDING_RETRY}),
    RunStatus.DONE: frozenset(),
    RunStatus.SKIPPED: frozenset(),
}


class StateMachine:
    def transition(
        self,
        run: StageRun,
        target: RunStatus,
        *,
        recovery: bool = False,
        reason: str | None = None,
        now: str | None = None,
    ) -> StageRun:
        if target not in ALLOWED_TRANSITIONS[run.status]:
            raise StateTransitionError(
                "STATE_TRANSITION_INVALID",
                f"Cannot transition {run.status.value} to {target.value}",
                evidence={"run_id": run.id, "source": run.status.value, "target": target.value},
            )
        if run.status is RunStatus.RUNNING and target is RunStatus.PENDING_RETRY and not recovery:
            raise StateTransitionError(
                "STATE_RECOVERY_REQUIRED",
                "running can move directly to pending_retry only during recovery",
            )
        if target is RunStatus.SKIPPED and not reason:
            raise StateTransitionError("SKIP_REASON_REQUIRED", "Skipping a run requires a reason")
        if run.status is RunStatus.BLOCKED:
            expected = RunStatus.PENDING if run.attempt == 0 else RunStatus.PENDING_RETRY
            if target is not expected:
                raise StateTransitionError(
                    "UNBLOCK_TARGET_INVALID",
                    f"Blocked run with attempt={run.attempt} must return to {expected.value}",
                )

        timestamp = now or utc_now()
        attempt = run.attempt
        started_at = run.started_at
        finished_at = run.finished_at
        if target is RunStatus.RUNNING:
            if attempt >= run.max_attempts:
                raise StateTransitionError(
                    "RETRY_EXHAUSTED",
                    f"Run {run.id} exhausted its {run.max_attempts} attempts",
                )
            attempt += 1
            started_at = timestamp
            finished_at = None
        elif target in {RunStatus.DONE, RunStatus.FAILED, RunStatus.SKIPPED}:
            finished_at = timestamp
        elif target in {RunStatus.PENDING, RunStatus.PENDING_RETRY}:
            finished_at = None

        return replace(
            run,
            status=target,
            attempt=attempt,
            started_at=started_at,
            finished_at=finished_at,
            updated_at=timestamp,
        )

    def new_run(
        self,
        *,
        project_id: str,
        stage_id: str,
        unit_id: str,
        input_hash: str,
        max_attempts: int,
        version: int = 1,
        supersedes_run_id: str | None = None,
        now: str | None = None,
    ) -> StageRun:
        timestamp = now or utc_now()
        return StageRun(
            id=new_id(),
            project_id=project_id,
            stage_id=stage_id,
            unit_id=unit_id,
            status=RunStatus.PENDING,
            input_hash=input_hash,
            idempotency_key=idempotency_key(
                project_id=project_id,
                stage_id=stage_id,
                unit_id=unit_id,
                input_hash=input_hash,
                version=version,
            ),
            version=version,
            attempt=0,
            max_attempts=max_attempts,
            started_at=None,
            finished_at=None,
            created_at=timestamp,
            updated_at=timestamp,
            supersedes_run_id=supersedes_run_id,
        )

    def reprocess(self, run: StageRun, *, input_hash: str | None = None) -> StageRun:
        if run.status not in {RunStatus.DONE, RunStatus.SKIPPED}:
            raise StateTransitionError(
                "REPROCESS_SOURCE_INVALID", "Only a terminal successful run can be reprocessed"
            )
        return self.new_run(
            project_id=run.project_id,
            stage_id=run.stage_id,
            unit_id=run.unit_id,
            input_hash=input_hash or run.input_hash,
            max_attempts=run.max_attempts,
            version=run.version + 1,
            supersedes_run_id=run.id,
        )

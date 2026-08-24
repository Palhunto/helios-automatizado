from dataclasses import replace

import pytest

from ebook_pipeline.core.errors import StateTransitionError
from ebook_pipeline.core.hashing import sha256_bytes
from ebook_pipeline.core.models import RunStatus
from ebook_pipeline.core.state_machine import StateMachine


def pending_run(max_attempts: int = 2):  # type: ignore[no-untyped-def]
    return StateMachine().new_run(
        project_id="00000000-0000-4000-8000-000000000001",
        stage_id="stage",
        unit_id="stage",
        input_hash=sha256_bytes(b"input"),
        max_attempts=max_attempts,
        now="2026-08-21T00:00:00+00:00",
    )


def test_happy_path_and_done_is_terminal() -> None:
    machine = StateMachine()
    pending = pending_run()
    running = machine.transition(pending, RunStatus.RUNNING)
    done = machine.transition(running, RunStatus.DONE)
    assert running.attempt == 1
    assert done.finished_at is not None
    with pytest.raises(StateTransitionError):
        machine.transition(done, RunStatus.RUNNING)


def test_running_to_pending_retry_requires_recovery() -> None:
    machine = StateMachine()
    running = machine.transition(pending_run(), RunStatus.RUNNING)
    with pytest.raises(StateTransitionError, match="only during recovery"):
        machine.transition(running, RunStatus.PENDING_RETRY)
    retry = machine.transition(running, RunStatus.PENDING_RETRY, recovery=True)
    assert retry.status is RunStatus.PENDING_RETRY


def test_retry_limit_is_enforced() -> None:
    machine = StateMachine()
    run = machine.transition(pending_run(max_attempts=1), RunStatus.RUNNING)
    failed = machine.transition(run, RunStatus.FAILED)
    retry = machine.transition(failed, RunStatus.PENDING_RETRY)
    with pytest.raises(StateTransitionError, match="exhausted"):
        machine.transition(retry, RunStatus.RUNNING)


def test_skip_requires_reason() -> None:
    with pytest.raises(StateTransitionError, match="requires a reason"):
        StateMachine().transition(pending_run(), RunStatus.SKIPPED)


def test_reprocess_creates_new_version_without_mutating_done() -> None:
    machine = StateMachine()
    done = machine.transition(machine.transition(pending_run(), RunStatus.RUNNING), RunStatus.DONE)
    replacement = machine.reprocess(done)
    assert done.status is RunStatus.DONE
    assert replacement.version == done.version + 1
    assert replacement.supersedes_run_id == done.id
    assert replacement.idempotency_key != done.idempotency_key


def test_reprocess_rejects_nonterminal_run() -> None:
    with pytest.raises(StateTransitionError) as captured:
        StateMachine().reprocess(pending_run())
    assert captured.value.code == "REPROCESS_SOURCE_INVALID"


def test_blocked_unblock_target_depends_on_attempt() -> None:
    machine = StateMachine()
    blocked_before_run = machine.transition(pending_run(), RunStatus.BLOCKED)
    assert machine.transition(blocked_before_run, RunStatus.PENDING).status is RunStatus.PENDING
    attempted = replace(blocked_before_run, attempt=1)
    with pytest.raises(StateTransitionError):
        machine.transition(attempted, RunStatus.PENDING)

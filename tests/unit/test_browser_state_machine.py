from __future__ import annotations

from dataclasses import replace

import pytest

from ebook_pipeline.browser.fingerprints import strict_request_text, transport_fingerprint
from ebook_pipeline.browser.models import BrowserInteraction, InteractionKind, InteractionStatus
from ebook_pipeline.browser.state_machine import BrowserInteractionStateMachine
from ebook_pipeline.core.errors import IntegrityError, StateTransitionError


def _interaction() -> BrowserInteraction:
    return BrowserInteraction(
        id="interaction",
        project_id="project",
        conversation_id="conversation",
        context_id="context",
        stage_run_id="run",
        kind=InteractionKind.CONTEXT_LOAD,
        unit_id=None,
        preparation_id=None,
        status=InteractionStatus.PREPARED,
        attempt=0,
        max_attempts=2,
        request_artifact_id="artifact",
        request_sha256="a" * 64,
        transport_fingerprint="b" * 64,
        response_artifact_id=None,
        response_sha256=None,
        capture_method_version=None,
        imported_entity_type=None,
        imported_entity_id=None,
        sent_at=None,
        captured_at=None,
        imported_at=None,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
        supersedes_interaction_id=None,
    )


def test_transport_fingerprint_is_minimal_and_artifact_decode_is_strict() -> None:
    assert transport_fingerprint("A\u00a0B\r\nC  ") == transport_fingerprint("A B\nC")
    assert transport_fingerprint("A B") != transport_fingerprint("a b")
    with pytest.raises(IntegrityError):
        strict_request_text(b"\xff")


def test_interaction_state_requires_capture_and_import_evidence() -> None:
    machine = BrowserInteractionStateMachine()
    sending = machine.transition(_interaction(), InteractionStatus.SENDING)
    sent = machine.transition(sending, InteractionStatus.SENT)
    with pytest.raises(StateTransitionError, match="verified response artifact"):
        machine.transition(sent, InteractionStatus.CAPTURED)
    evidenced = replace(
        sent,
        response_artifact_id="response",
        response_sha256="c" * 64,
        capture_method_version="rendered_text_v1",
    )
    captured = machine.transition(evidenced, InteractionStatus.CAPTURED)
    with pytest.raises(StateTransitionError, match="persisted M2 result"):
        machine.transition(captured, InteractionStatus.IMPORTED)
    imported = machine.transition(
        replace(
            captured,
            imported_entity_type="writing_acknowledgement",
            imported_entity_id="ack",
        ),
        InteractionStatus.IMPORTED,
    )
    with pytest.raises(StateTransitionError):
        machine.transition(imported, InteractionStatus.SENDING)


def test_interaction_retry_limit_is_enforced() -> None:
    machine = BrowserInteractionStateMachine()
    exhausted = replace(_interaction(), attempt=2)
    with pytest.raises(StateTransitionError, match="retry limit"):
        machine.transition(exhausted, InteractionStatus.SENDING)


def test_new_lineage_first_attempt_is_not_counted_twice() -> None:
    machine = BrowserInteractionStateMachine()
    lineage = replace(
        _interaction(),
        attempt=1,
        max_attempts=1,
        supersedes_interaction_id="abandoned-interaction",
    )

    sending = machine.transition(
        lineage,
        InteractionStatus.SENDING,
        attempt_already_counted=True,
    )

    assert sending.status is InteractionStatus.SENDING
    assert sending.attempt == 1


def test_blocked_can_resume_as_sent_only_from_recovery_evidence() -> None:
    machine = BrowserInteractionStateMachine()
    legacy_sent_at = "2026-01-01T00:00:01+00:00"
    blocked = replace(
        _interaction(),
        status=InteractionStatus.BLOCKED,
        attempt=1,
        sent_at=legacy_sent_at,
    )
    with pytest.raises(StateTransitionError):
        machine.transition(blocked, InteractionStatus.SENT)
    resumed = machine.transition(blocked, InteractionStatus.SENT, recovery=True)
    assert resumed.status is InteractionStatus.SENT
    assert resumed.sent_at == legacy_sent_at

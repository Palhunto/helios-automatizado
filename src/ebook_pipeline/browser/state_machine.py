from __future__ import annotations

from dataclasses import replace

from ebook_pipeline.browser.models import BrowserInteraction, InteractionStatus
from ebook_pipeline.core.errors import StateTransitionError
from ebook_pipeline.core.ids import utc_now

_ALLOWED = {
    InteractionStatus.PREPARED: {
        InteractionStatus.SENDING,
        InteractionStatus.BLOCKED,
        InteractionStatus.FAILED,
    },
    InteractionStatus.SENDING: {
        InteractionStatus.SENT,
        InteractionStatus.STREAMING,
        InteractionStatus.BLOCKED,
        InteractionStatus.FAILED,
    },
    InteractionStatus.SENT: {
        InteractionStatus.STREAMING,
        InteractionStatus.CAPTURED,
        InteractionStatus.BLOCKED,
        InteractionStatus.FAILED,
    },
    InteractionStatus.STREAMING: {
        InteractionStatus.CAPTURED,
        InteractionStatus.BLOCKED,
        InteractionStatus.FAILED,
    },
    InteractionStatus.CAPTURED: {
        InteractionStatus.IMPORTED,
        InteractionStatus.BLOCKED,
        InteractionStatus.FAILED,
    },
    InteractionStatus.IMPORTED: set(),
    InteractionStatus.FAILED: set(),
    InteractionStatus.BLOCKED: set(),
}


class BrowserInteractionStateMachine:
    def transition(
        self,
        interaction: BrowserInteraction,
        target: InteractionStatus,
        *,
        recovery: bool = False,
        attempt_already_counted: bool = False,
    ) -> BrowserInteraction:
        recovery_resume = (
            recovery
            and interaction.status is InteractionStatus.BLOCKED
            and target is InteractionStatus.SENT
        )
        if target not in _ALLOWED[interaction.status] and not recovery_resume:
            raise StateTransitionError(
                "BROWSER_TRANSITION_INVALID",
                f"Cannot transition browser interaction from {interaction.status} to {target}",
            )
        if (
            target is InteractionStatus.SENDING
            and not attempt_already_counted
            and interaction.attempt >= interaction.max_attempts
        ):
            raise StateTransitionError(
                "BROWSER_ATTEMPTS_EXHAUSTED", "Browser interaction retry limit was reached"
            )
        if target is InteractionStatus.CAPTURED and (
            interaction.response_artifact_id is None
            or interaction.response_sha256 is None
            or interaction.capture_method_version is None
        ):
            raise StateTransitionError(
                "BROWSER_CAPTURE_EVIDENCE_MISSING",
                "Captured requires a verified response artifact and capture method",
            )
        if target is InteractionStatus.IMPORTED and (
            interaction.imported_entity_type is None or interaction.imported_entity_id is None
        ):
            raise StateTransitionError(
                "BROWSER_IMPORT_EVIDENCE_MISSING",
                "Imported requires the persisted M2 result identity",
            )
        now = utc_now()
        return replace(
            interaction,
            status=target,
            attempt=(
                interaction.attempt + 1
                if target is InteractionStatus.SENDING and not attempt_already_counted
                else interaction.attempt
            ),
            sent_at=(
                now
                if target is InteractionStatus.SENT and interaction.sent_at is None
                else interaction.sent_at
            ),
            captured_at=(now if target is InteractionStatus.CAPTURED else interaction.captured_at),
            imported_at=(now if target is InteractionStatus.IMPORTED else interaction.imported_at),
            updated_at=now,
        )

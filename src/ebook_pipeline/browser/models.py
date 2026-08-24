from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ConversationStatus(StrEnum):
    PROVISIONING = "provisioning"
    READY = "ready"
    BLOCKED = "blocked"


class InteractionKind(StrEnum):
    CONTEXT_LOAD = "context_load"
    UNIT_REQUEST = "unit_request"


class InteractionStatus(StrEnum):
    PREPARED = "prepared"
    SENDING = "sending"
    SENT = "sent"
    STREAMING = "streaming"
    CAPTURED = "captured"
    IMPORTED = "imported"
    FAILED = "failed"
    BLOCKED = "blocked"


class InteractionResolutionKind(StrEnum):
    OPERATOR_ABANDONED = "operator_abandoned"


class ConversationResolutionAction(StrEnum):
    RETAINED = "conversation_retained"
    PROVISIONING_ATTEMPT_CLOSED = "provisioning_attempt_closed"


class SessionState(StrEnum):
    READY = "ready"
    LOGIN_REQUIRED = "login_required"
    CHALLENGE = "challenge"
    EXPIRED = "expired"


class TurnState(StrEnum):
    NOT_SENT = "not_sent"
    STREAMING = "streaming"
    COMPLETE = "complete"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True, slots=True)
class BrowserConversation:
    id: str
    project_id: str
    context_id: str
    provider: str
    status: ConversationStatus
    conversation_path: str | None
    first_turn_fingerprint: str | None
    provisioning_baseline_json: str | None
    provisioning_started_at: str
    ready_at: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class BrowserInteraction:
    id: str
    project_id: str
    conversation_id: str
    context_id: str
    stage_run_id: str
    kind: InteractionKind
    unit_id: str | None
    preparation_id: str | None
    status: InteractionStatus
    attempt: int
    max_attempts: int
    request_artifact_id: str
    request_sha256: str
    transport_fingerprint: str
    response_artifact_id: str | None
    response_sha256: str | None
    capture_method_version: str | None
    imported_entity_type: str | None
    imported_entity_id: str | None
    sent_at: str | None
    captured_at: str | None
    imported_at: str | None
    created_at: str
    updated_at: str
    supersedes_interaction_id: str | None


@dataclass(frozen=True, slots=True)
class BrowserInteractionResolution:
    id: str
    interaction_id: str
    project_id: str
    resolution: InteractionResolutionKind
    operator: str
    reason: str
    conversation_action: ConversationResolutionAction
    evidence_json: str
    created_at: str


@dataclass(frozen=True, slots=True)
class BrowserConversationInvalidation:
    id: str
    conversation_id: str
    project_id: str
    replacement_conversation_id: str
    reason: str
    evidence_json: str
    created_at: str


@dataclass(frozen=True, slots=True)
class TurnInspection:
    state: TurnState
    conversation_path: str | None = None
    response_text: str | None = None
    evidence: dict[str, object] | None = None
    observed_user_turn_fingerprint: str | None = None


@dataclass(frozen=True, slots=True)
class BootstrapCandidate:
    conversation_path: str
    user_turn_fingerprint: str
    observed_at: str


@dataclass(frozen=True, slots=True)
class CaptureSpikeResult:
    sample_kind: str
    rendered_sha256: str
    copied_sha256: str | None
    equivalent: bool
    selected_method_version: str
    comparison: dict[str, object]


@dataclass(frozen=True, slots=True)
class ComposerProbeCaseResult:
    expected_length: int
    observed_length: int
    expected_fingerprint: str
    observed_fingerprint: str
    stable_read_1: str
    stable_read_2: str
    editor_metadata: dict[str, object]

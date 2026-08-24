from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from ebook_pipeline.browser.models import (
    BootstrapCandidate,
    CaptureSpikeResult,
    SessionState,
    TurnInspection,
)


class ChatProviderAdapter(Protocol):
    def ensure_ready(self) -> SessionState: ...

    def create_conversation(self) -> None: ...

    def open_conversation(self, conversation_path: str) -> None: ...

    def reload_conversation(self, conversation_path: str) -> None: ...

    def list_conversation_paths(self) -> tuple[str, ...]: ...

    def send_message(
        self,
        text: str,
        *,
        on_send_attempt_started: Callable[[], None] | None = None,
    ) -> str | None: ...

    def current_conversation_path(self) -> str | None: ...

    def pre_send_turn_anchor(self) -> dict[str, object] | None: ...

    def inspect_turn(self, transport_fingerprint: str) -> TurnInspection: ...

    def capture_response(self, transport_fingerprint: str) -> str: ...

    def inspect_sent_turn_structure(
        self, transport_fingerprint: str
    ) -> TurnInspection: ...

    def capture_structural_response(self, transport_fingerprint: str) -> str: ...

    def inspect_reconciliation_turn(
        self, transport_fingerprint: str
    ) -> TurnInspection: ...

    def capture_reconciled_response(self, transport_fingerprint: str) -> str: ...

    def inspect_reconciliation_unit_turn(
        self,
        turn_binding: int | dict[str, object],
        expected_conversation_path: str | None = None,
    ) -> TurnInspection: ...

    def capture_reconciled_unit_response(
        self, turn_binding: int | dict[str, object]
    ) -> str: ...

    def recapture_persisted_unit_response(self, user_turn_ordinal: int) -> str: ...

    def find_reconciliation_candidates(
        self, transport_fingerprint: str, started_at: str
    ) -> tuple[BootstrapCandidate, ...]: ...

    def find_bootstrap_candidates(
        self, transport_fingerprint: str, started_at: str
    ) -> tuple[BootstrapCandidate, ...]: ...

    def capture_spike(self, sample_kind: str) -> CaptureSpikeResult: ...

    def capture_gate_ready(self) -> bool: ...

    def close(self) -> None: ...

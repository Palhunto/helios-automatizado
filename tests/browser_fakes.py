from __future__ import annotations

from collections.abc import Callable

from ebook_pipeline.browser.fingerprints import transport_fingerprint
from ebook_pipeline.browser.models import (
    BootstrapCandidate,
    CaptureSpikeResult,
    SessionState,
    TurnInspection,
    TurnState,
)
from ebook_pipeline.core.errors import ConflictError
from ebook_pipeline.core.ids import utc_now

DEFAULT_CONVERSATION_PATH = "/c/9491397d-073e-4cc8-a1b9-a38a8968cfaf"
OLD_CONVERSATION_PATH = "/c/3a615ed1-ab7d-449e-aeeb-80eb29fc615f"


class FakeChatAdapter:
    def __init__(self) -> None:
        self.session_state = SessionState.READY
        self.gate_ready = True
        self.current_path: str | None = None
        self.next_path = DEFAULT_CONVERSATION_PATH
        self.next_response = "Contexto compreendido."
        self.conversations: dict[str, dict[str, str]] = {OLD_CONVERSATION_PATH: {}}
        self.sent_messages: list[tuple[str, str]] = []
        self.inspect_override: TurnState | None = None
        self.inspection_path_override: str | None = None
        self.bootstrap_override: tuple[BootstrapCandidate, ...] | None = None
        self.structural_baseline_available = False
        self.structural_stable_reads = 0
        self.unit_reconciliation_stable_reads = 0
        self.next_send_uses_big_paste = False
        self.recaptured_ordinals: list[int] = []
        self.reloaded_paths: list[str] = []
        self.reconciliation_bindings: list[int | dict[str, object]] = []
        self._pre_send_turn_anchor: dict[str, object] | None = None

    def ensure_ready(self) -> SessionState:
        return self.session_state

    def create_conversation(self) -> None:
        self.current_path = None
        self.structural_baseline_available = False
        self.unit_reconciliation_stable_reads = 0
        self._pre_send_turn_anchor = None

    def open_conversation(self, conversation_path: str) -> None:
        self.current_path = conversation_path
        self.structural_baseline_available = False
        self.unit_reconciliation_stable_reads = 0
        self._pre_send_turn_anchor = None

    def reload_conversation(self, conversation_path: str) -> None:
        if self.current_path != conversation_path:
            raise ConflictError(
                "BROWSER_CONVERSATION_RELOAD_WRONG_PATH",
                "Fake reload requires the expected conversation to be current",
            )
        self.reloaded_paths.append(conversation_path)
        self.structural_baseline_available = False
        self.unit_reconciliation_stable_reads = 0
        self._pre_send_turn_anchor = None

    def list_conversation_paths(self) -> tuple[str, ...]:
        return tuple(sorted(self.conversations))

    def send_message(
        self,
        text: str,
        *,
        on_send_attempt_started: Callable[[], None] | None = None,
    ) -> str | None:
        self.structural_baseline_available = True
        self.structural_stable_reads = 0
        path = self.current_path or self.next_path
        existing = list(self.conversations.get(path, {}))
        tail: list[dict[str, str]] = []
        if existing:
            previous = existing[-1]
            tail = [
                {"role": "user", "id": f"user-{previous}"},
                {"role": "assistant", "id": f"assistant-{previous}"},
            ]
        self._pre_send_turn_anchor = {
            "version": 1,
            "conversation_path": self.current_path,
            "pre_send_user_turn_count": len(existing),
            "pre_send_assistant_turn_count": len(existing),
            "tail": tail,
        }
        if on_send_attempt_started is not None:
            on_send_attempt_started()
        self.current_path = path
        fingerprint = transport_fingerprint(text)
        self.conversations.setdefault(path, {})[fingerprint] = self.next_response
        self.sent_messages.append((path, text))
        return path

    def current_conversation_path(self) -> str | None:
        return self.current_path

    def pre_send_turn_anchor(self) -> dict[str, object] | None:
        return (
            None
            if self._pre_send_turn_anchor is None
            else dict(self._pre_send_turn_anchor)
        )

    def inspect_turn(self, fingerprint: str) -> TurnInspection:
        if self.inspect_override is not None:
            return TurnInspection(
                self.inspect_override,
                self.inspection_path_override or self.current_path,
                observed_user_turn_fingerprint=(
                    fingerprint
                    if self.inspect_override in {TurnState.STREAMING, TurnState.COMPLETE}
                    else None
                ),
            )
        matches = [
            (path, response)
            for path, turns in self.conversations.items()
            for candidate, response in turns.items()
            if candidate == fingerprint
        ]
        if not matches:
            return TurnInspection(TurnState.NOT_SENT, self.current_path)
        current = [item for item in matches if item[0] == self.current_path]
        if len(current) != 1:
            return TurnInspection(TurnState.AMBIGUOUS, self.current_path)
        return TurnInspection(
            TurnState.COMPLETE,
            self.inspection_path_override or self.current_path,
            current[0][1],
            observed_user_turn_fingerprint=fingerprint,
        )

    def capture_response(self, fingerprint: str) -> str:
        inspection = self.inspect_turn(fingerprint)
        assert inspection.response_text is not None
        return inspection.response_text

    def inspect_sent_turn_structure(self, fingerprint: str) -> TurnInspection:
        if not self.structural_baseline_available:
            raise ConflictError(
                "BROWSER_SEND_REQUIRES_RECONCILE",
                "The in-process pre-Send user-turn baseline is unavailable",
            )
        if self.inspect_override is not None:
            inspection = TurnInspection(
                self.inspect_override,
                self.inspection_path_override or self.current_path,
                observed_user_turn_fingerprint=(
                    fingerprint
                    if self.inspect_override in {TurnState.STREAMING, TurnState.COMPLETE}
                    else None
                ),
            )
        else:
            matches = [
                (path, response)
                for path, turns in self.conversations.items()
                for candidate, response in turns.items()
                if candidate == fingerprint and path == self.current_path
            ]
            inspection = (
                TurnInspection(TurnState.NOT_SENT, self.current_path)
                if len(matches) == 0
                else TurnInspection(
                    TurnState.COMPLETE,
                    self.inspection_path_override or self.current_path,
                    matches[0][1],
                    observed_user_turn_fingerprint=fingerprint,
                )
                if len(matches) == 1
                else TurnInspection(TurnState.AMBIGUOUS, self.current_path)
            )
        if inspection.state in {TurnState.STREAMING, TurnState.COMPLETE}:
            self.structural_stable_reads = 2
        return inspection

    def capture_structural_response(self, fingerprint: str) -> str:
        matches = [
            response
            for path, turns in self.conversations.items()
            for candidate, response in turns.items()
            if path == self.current_path and candidate == fingerprint
        ]
        assert len(matches) == 1
        return matches[0]

    def inspect_reconciliation_turn(self, fingerprint: str) -> TurnInspection:
        return self.inspect_turn(fingerprint)

    def capture_reconciled_response(self, fingerprint: str) -> str:
        return self.capture_response(fingerprint)

    def inspect_reconciliation_unit_turn(
        self,
        turn_binding: int | dict[str, object],
        expected_conversation_path: str | None = None,
    ) -> TurnInspection:
        self.reconciliation_bindings.append(turn_binding)
        raw_ordinal = (
            turn_binding.get("pre_send_user_turn_count")
            if isinstance(turn_binding, dict)
            else turn_binding
        )
        assert isinstance(raw_ordinal, int)
        user_turn_ordinal = raw_ordinal
        if (
            expected_conversation_path is not None
            and self.current_path != expected_conversation_path
        ):
            return TurnInspection(TurnState.NOT_SENT, self.current_path)
        if self.current_path is None:
            return TurnInspection(TurnState.NOT_SENT, None)
        turns = list(self.conversations.get(self.current_path, {}).items())
        expected_count = user_turn_ordinal + 1
        evidence: dict[str, object] = {
            "user_turn_ordinal": user_turn_ordinal,
            "user_turn_candidate_count": len(turns),
            "expected_user_turn_count": expected_count,
        }
        if len(turns) > expected_count:
            return TurnInspection(TurnState.AMBIGUOUS, self.current_path, evidence=evidence)
        if len(turns) < expected_count:
            self.unit_reconciliation_stable_reads = 0
            return TurnInspection(TurnState.NOT_SENT, self.current_path, evidence=evidence)
        self.unit_reconciliation_stable_reads += 1
        if self.unit_reconciliation_stable_reads < 2:
            return TurnInspection(TurnState.NOT_SENT, self.current_path, evidence=evidence)
        if self.inspect_override is not None:
            return TurnInspection(self.inspect_override, self.current_path, evidence=evidence)
        response = turns[user_turn_ordinal][1]
        return TurnInspection(
            TurnState.COMPLETE if response.strip() else TurnState.AMBIGUOUS,
            self.current_path,
            response,
            evidence=evidence,
        )

    def capture_reconciled_unit_response(
        self, turn_binding: int | dict[str, object]
    ) -> str:
        raw_ordinal = (
            turn_binding.get("pre_send_user_turn_count")
            if isinstance(turn_binding, dict)
            else turn_binding
        )
        assert isinstance(raw_ordinal, int)
        user_turn_ordinal = raw_ordinal
        assert self.current_path is not None
        turns = list(self.conversations[self.current_path].items())
        assert self.unit_reconciliation_stable_reads >= 2
        return turns[user_turn_ordinal][1]

    def recapture_persisted_unit_response(self, user_turn_ordinal: int) -> str:
        assert self.current_path is not None
        self.recaptured_ordinals.append(user_turn_ordinal)
        turns = list(self.conversations[self.current_path].items())
        return turns[user_turn_ordinal][1]

    def find_reconciliation_candidates(
        self, fingerprint: str, started_at: str
    ) -> tuple[BootstrapCandidate, ...]:
        return self.find_bootstrap_candidates(fingerprint, started_at)

    def find_bootstrap_candidates(
        self, fingerprint: str, started_at: str
    ) -> tuple[BootstrapCandidate, ...]:
        del started_at
        if self.bootstrap_override is not None:
            return self.bootstrap_override
        return tuple(
            BootstrapCandidate(path, fingerprint, utc_now())
            for path, turns in self.conversations.items()
            if fingerprint in turns
        )

    def capture_spike(self, sample_kind: str) -> CaptureSpikeResult:
        return CaptureSpikeResult(
            sample_kind, "a" * 64, "a" * 64, True, "rendered_text_v1", {}
        )

    def capture_gate_ready(self) -> bool:
        return self.gate_ready

    def close(self) -> None:
        return None

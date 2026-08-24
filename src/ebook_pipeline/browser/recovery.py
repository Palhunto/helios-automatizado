from __future__ import annotations

import json

from ebook_pipeline.browser.models import ConversationStatus, InteractionStatus
from ebook_pipeline.browser.repositories import (
    BrowserConversationRepository,
    BrowserInteractionRepository,
    BrowserInteractionResolutionRepository,
)
from ebook_pipeline.browser.service import BrowserAutomationService
from ebook_pipeline.browser.urls import is_real_conversation_path
from ebook_pipeline.core.errors import HeliosError, IntegrityError

_INCOMPLETE = {
    InteractionStatus.PREPARED,
    InteractionStatus.SENDING,
    InteractionStatus.SENT,
    InteractionStatus.STREAMING,
    InteractionStatus.CAPTURED,
}


class BrowserRecovery:
    def __init__(self, service: BrowserAutomationService) -> None:
        self.service = service

    def recover(self, project_id: str) -> dict[str, object]:
        with self.service.database.connection() as connection:
            interactions = BrowserInteractionRepository(connection).list_for_project(project_id)
        examined: list[dict[str, object]] = []
        recovered: list[dict[str, object]] = []
        skipped: list[dict[str, object]] = []
        for item in interactions:
            if item.status not in _INCOMPLETE | {
                InteractionStatus.BLOCKED,
                InteractionStatus.FAILED,
            }:
                continue
            examined_item: dict[str, object] = {
                "interaction_id": item.id,
                "status": item.status.value,
                "kind": item.kind.value,
                "unit_id": item.unit_id,
            }
            examined.append(examined_item)
            if item.status is InteractionStatus.BLOCKED:
                try:
                    outcome = self._recover_blocked(item.id)
                except HeliosError as exc:
                    skipped.append(
                        {
                            **examined_item,
                            "reason": "reprobe_failed_closed",
                            "error_code": exc.code,
                            "message": exc.message,
                            "requires_action": True,
                        }
                    )
                    continue
                if outcome.get("outcome") == "recovered":
                    result = outcome.get("result")
                    if isinstance(result, dict):
                        recovered.append(result)
                    continue
                skipped.append({**examined_item, **outcome})
                continue
            if item.status is InteractionStatus.FAILED:
                skipped.append(
                    {
                        **examined_item,
                        "reason": "terminal_failed",
                        "requires_action": True,
                    }
                )
                continue
            if item.status is InteractionStatus.PREPARED:
                skipped.append(
                    {
                        **examined_item,
                        "reason": "not_started_requires_explicit_run",
                        "requires_action": True,
                    }
                )
                continue
            try:
                recovered.append(self._recover_one(item.id))
            except HeliosError as exc:
                skipped.append(
                    {
                        **examined_item,
                        "reason": "recovery_failed_closed",
                        "error_code": exc.code,
                        "message": exc.message,
                        "requires_action": True,
                    }
                )
        return {
            "project_id": project_id,
            "examined": examined,
            "recovered": recovered,
            "skipped": skipped,
        }

    def _recover_blocked(self, interaction_id: str) -> dict[str, object]:
        with self.service.database.connection() as connection:
            interactions = BrowserInteractionRepository(connection)
            item = interactions.get(interaction_id)
            conversation = BrowserConversationRepository(connection).get(item.conversation_id)
            resolution = BrowserInteractionResolutionRepository(connection).for_interaction(
                item.id
            )
            events = interactions.events(item.id)
            error_code = self._blocking_error_code(events)
        if resolution is not None:
            return {
                "outcome": "skipped",
                "reason": "operator_abandoned",
                "resolution_id": resolution.id,
                "requires_action": False,
            }
        local_capture_proof = (
            self.service._blocked_local_unit_capture_recovery_proof(  # noqa: SLF001
                item, events, resolution
            )
        )
        if local_capture_proof is not None:
            reconciled = self.service.reconcile_blocked_interaction(
                item.project_id, item.id
            )
            result = reconciled.get("result")
            return {
                "outcome": "recovered",
                "interaction_id": item.id,
                "result": result if isinstance(result, dict) else reconciled,
            }
        if error_code != "BROWSER_SEND_NOT_PROVABLE":
            return {
                "outcome": "skipped",
                "reason": "human_resolution_required",
                "error_code": error_code,
                "requires_action": True,
            }
        if (
            conversation.status is not ConversationStatus.READY
            or conversation.conversation_path is None
            or conversation.first_turn_fingerprint != item.transport_fingerprint
        ):
            return {
                "outcome": "skipped",
                "reason": "reprobe_evidence_incomplete",
                "error_code": error_code,
                "requires_action": True,
            }
        if not is_real_conversation_path(conversation.conversation_path):
            return {
                "outcome": "skipped",
                "reason": "invalid_conversation_path",
                "error_code": error_code,
                "requires_action": True,
            }
        result = self.service.reprobe_blocked_interaction(item.id)
        if result.get("outcome") == "recovered":
            return result
        return {**result, "error_code": error_code, "requires_action": True}

    def _recover_one(self, interaction_id: str) -> dict[str, object]:
        with self.service.database.connection() as connection:
            item = BrowserInteractionRepository(connection).get(interaction_id)
            conversation = BrowserConversationRepository(connection).get(item.conversation_id)
        if item.status is InteractionStatus.CAPTURED:
            return self.service.run_interaction(item.id)
        if conversation.conversation_path is None:
            if (
                conversation.status is not ConversationStatus.PROVISIONING
                or item.status is not InteractionStatus.SENDING
            ):
                self.service._block(  # noqa: SLF001 - same recovery domain boundary.
                    item,
                    "BROWSER_BOOTSTRAP_STATE_INVALID",
                    "Missing conversation path is not a recoverable bootstrap state",
                )
            baseline = self._baseline(conversation.provisioning_baseline_json)
            candidates = tuple(
                candidate
                for candidate in self.service.adapter.find_bootstrap_candidates(
                    item.transport_fingerprint, conversation.provisioning_started_at
                )
                if candidate.conversation_path not in baseline
                and candidate.user_turn_fingerprint == item.transport_fingerprint
            )
            if len(candidates) != 1:
                self.service._block(  # noqa: SLF001
                    item,
                    "BROWSER_BOOTSTRAP_AMBIGUOUS",
                    "Bootstrap recovery requires exactly one proven new conversation",
                    {"candidate_count": len(candidates)},
                )
            candidate = candidates[0]
            if not is_real_conversation_path(candidate.conversation_path):
                self.service._block(  # noqa: SLF001
                    item,
                    "BROWSER_CONVERSATION_PATH_INVALID",
                    "Bootstrap candidate is not a canonical /c/<uuid> path",
                )
            self.service.adapter.open_conversation(candidate.conversation_path)
        return self.service.run_interaction(item.id)

    @staticmethod
    def _blocking_error_code(events: list[dict[str, object]]) -> str | None:
        for event in reversed(events):
            if event.get("status") != InteractionStatus.BLOCKED.value:
                continue
            evidence = event.get("evidence")
            if isinstance(evidence, dict):
                code = evidence.get("error_code")
                if code is not None:
                    return str(code)
        return None

    @staticmethod
    def _baseline(value: str | None) -> set[str]:
        if value is None:
            raise IntegrityError(
                "BROWSER_BOOTSTRAP_BASELINE_MISSING",
                "Provisioning baseline is required to prove a new conversation",
            )
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise IntegrityError(
                "BROWSER_BOOTSTRAP_BASELINE_INVALID", "Provisioning baseline is invalid"
            ) from exc
        if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
            raise IntegrityError(
                "BROWSER_BOOTSTRAP_BASELINE_INVALID", "Provisioning baseline is invalid"
            )
        return set(parsed)

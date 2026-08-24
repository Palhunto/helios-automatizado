from __future__ import annotations

from typing import Protocol

from ebook_pipeline.browser.models import InteractionKind, InteractionStatus
from ebook_pipeline.browser.repositories import (
    BrowserConversationRepository,
    BrowserInteractionRepository,
)
from ebook_pipeline.browser.urls import is_real_conversation_path
from ebook_pipeline.config import AppConfig
from ebook_pipeline.core.errors import ConfigurationError, ConflictError, NotFoundError
from ebook_pipeline.storage.database import Database
from ebook_pipeline.storage.repositories import ProjectRepository


class UnitResponseProbeAdapter(Protocol):
    def unit_response_completion_probe(
        self, conversation_path: str, user_turn_ordinal: int
    ) -> dict[str, object]: ...


class UnitResponseProbeService:
    """Read-only completion audit for one persisted ordinal unit response."""

    def __init__(
        self,
        config: AppConfig,
        database: Database,
        adapter: UnitResponseProbeAdapter,
    ) -> None:
        self.config = config
        self.database = database
        self.adapter = adapter

    def run(self, project_id: str, interaction_id: str) -> dict[str, object]:
        if self.config.browser_channel != "chrome":
            raise ConfigurationError(
                "BROWSER_UNIT_RESPONSE_SPIKE_REQUIRES_CHROME",
                "Unit response spike requires browser channel 'chrome'",
            )
        with self.database.read_only_connection() as connection:
            ProjectRepository(connection).get(project_id)
            interactions = BrowserInteractionRepository(connection)
            item = interactions.get(interaction_id)
            if item.project_id != project_id:
                raise NotFoundError(
                    "BROWSER_INTERACTION_NOT_FOUND",
                    "Browser interaction was not found in the requested project",
                )
            if item.kind is not InteractionKind.UNIT_REQUEST:
                raise ConflictError(
                    "BROWSER_UNIT_RESPONSE_SPIKE_KIND_INVALID",
                    "Unit response spike requires a persisted unit_request interaction",
                )
            conversation = BrowserConversationRepository(connection).get(
                item.conversation_id
            )
            if (
                conversation.project_id != project_id
                or conversation.context_id != item.context_id
                or conversation.conversation_path is None
                or not is_real_conversation_path(conversation.conversation_path)
            ):
                raise ConflictError(
                    "BROWSER_UNIT_RESPONSE_SPIKE_BINDING_INVALID",
                    "Interaction is not bound to one canonical persisted conversation",
                )
            target_events = interactions.events(item.id)
            send_started = None
            for event in target_events:
                evidence = event.get("evidence")
                if (
                    event.get("status") == InteractionStatus.SENDING.value
                    and isinstance(evidence, dict)
                    and evidence.get("effect_boundary") == "send_attempt_started"
                ):
                    send_started = str(event["created_at"])
                    break
            if send_started is None:
                raise ConflictError(
                    "BROWSER_UNIT_RESPONSE_SPIKE_SEND_EVIDENCE_MISSING",
                    "Interaction has no persisted send_attempt_started boundary",
                )
            previous = []
            for candidate in interactions.list_for_conversation(conversation.id):
                if candidate.id == item.id:
                    continue
                candidate_events = interactions.events(candidate.id)
                proven_sent = (
                    candidate.context_id == item.context_id
                    and candidate.status
                    in {
                        InteractionStatus.SENT,
                        InteractionStatus.CAPTURED,
                        InteractionStatus.IMPORTED,
                    }
                    and candidate.sent_at is not None
                    and candidate.sent_at < send_started
                    and any(
                        event.get("status") == InteractionStatus.SENT.value
                        for event in candidate_events
                    )
                )
                if not proven_sent:
                    raise ConflictError(
                        "BROWSER_UNIT_RESPONSE_SPIKE_ORDINAL_AMBIGUOUS",
                        "Conversation contains an interaction not provably before the target",
                        evidence={"interaction_id": candidate.id},
                    )
                previous.append(candidate)
            user_turn_ordinal = len(previous)
            conversation_path = conversation.conversation_path

        evidence = self.adapter.unit_response_completion_probe(
            conversation_path, user_turn_ordinal
        )
        return {
            "project_id": project_id,
            "interaction_id": interaction_id,
            "unit_id": item.unit_id,
            "database_mode": "read_only",
            **evidence,
        }

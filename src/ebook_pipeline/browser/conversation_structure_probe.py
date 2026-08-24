from __future__ import annotations

from typing import Protocol

from ebook_pipeline.browser.repositories import (
    BrowserConversationRepository,
    BrowserInteractionRepository,
)
from ebook_pipeline.browser.urls import is_real_conversation_path
from ebook_pipeline.config import AppConfig
from ebook_pipeline.core.errors import ConfigurationError, ConflictError, NotFoundError
from ebook_pipeline.storage.database import Database
from ebook_pipeline.storage.repositories import ProjectRepository


class ConversationStructureProbeAdapter(Protocol):
    def conversation_structure_probe(
        self, conversation_path: str
    ) -> dict[str, object]: ...


class ConversationStructureProbeService:
    """Read-only structural audit of one persisted ChatGPT conversation."""

    def __init__(
        self,
        config: AppConfig,
        database: Database,
        adapter: ConversationStructureProbeAdapter,
    ) -> None:
        self.config = config
        self.database = database
        self.adapter = adapter

    def run(self, project_id: str, interaction_id: str) -> dict[str, object]:
        if self.config.browser_channel != "chrome":
            raise ConfigurationError(
                "BROWSER_CONVERSATION_STRUCTURE_SPIKE_REQUIRES_CHROME",
                "Conversation structure spike requires browser channel 'chrome'",
            )
        with self.database.read_only_connection() as connection:
            ProjectRepository(connection).get(project_id)
            item = BrowserInteractionRepository(connection).get(interaction_id)
            if item.project_id != project_id:
                raise NotFoundError(
                    "BROWSER_INTERACTION_NOT_FOUND",
                    "Browser interaction was not found in the requested project",
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
                    "BROWSER_CONVERSATION_STRUCTURE_SPIKE_BINDING_INVALID",
                    "Interaction is not bound to one canonical persisted conversation",
                )
            conversation_path = conversation.conversation_path

        evidence = self.adapter.conversation_structure_probe(conversation_path)
        return {
            "project_id": project_id,
            "interaction_id": interaction_id,
            "unit_id": item.unit_id,
            "database_mode": "read_only",
            **evidence,
        }

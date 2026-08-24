from __future__ import annotations

from typing import Protocol
from urllib.parse import urlparse

from ebook_pipeline.browser.urls import conversation_path_from_page_url
from ebook_pipeline.config import AppConfig
from ebook_pipeline.core.errors import ConfigurationError, IntegrityError
from ebook_pipeline.storage.database import Database
from ebook_pipeline.storage.repositories import ProjectRepository


class AssistantResponseProbeAdapter(Protocol):
    def assistant_response_text_probe(
        self,
        conversation_path: str,
        assistant_turn_ordinal: int,
        expected_prefix: str,
    ) -> dict[str, object]: ...


class AssistantResponseProbeService:
    """Read-only capture-scope verification for one existing assistant response."""

    def __init__(
        self,
        config: AppConfig,
        database: Database,
        adapter: AssistantResponseProbeAdapter,
    ) -> None:
        self.config = config
        self.database = database
        self.adapter = adapter

    def run(
        self,
        project_id: str,
        conversation_url: str,
        assistant_turn_ordinal: int,
        expected_prefix: str,
    ) -> dict[str, object]:
        if self.config.browser_channel != "chrome":
            raise ConfigurationError(
                "BROWSER_ASSISTANT_RESPONSE_SPIKE_REQUIRES_CHROME",
                "Assistant response spike requires browser channel 'chrome'",
            )
        parsed = urlparse(conversation_url)
        conversation_path = conversation_path_from_page_url(conversation_url)
        if (
            conversation_path is None
            or parsed.query
            or parsed.fragment
            or conversation_url != f"https://chatgpt.com{conversation_path}"
        ):
            raise IntegrityError(
                "BROWSER_CONVERSATION_PATH_INVALID",
                "Conversation URL is not a canonical https://chatgpt.com/c/<uuid> URL",
            )
        if assistant_turn_ordinal < 0 or not expected_prefix:
            raise IntegrityError(
                "BROWSER_ASSISTANT_RESPONSE_SPIKE_INPUT_INVALID",
                "Assistant response spike requires a non-negative ordinal and prefix",
            )
        with self.database.read_only_connection() as connection:
            project = ProjectRepository(connection).get(project_id)
        evidence = self.adapter.assistant_response_text_probe(
            conversation_path, assistant_turn_ordinal, expected_prefix
        )
        return {
            "project_id": project.id,
            "conversation_url": conversation_url,
            "database_mode": "read_only",
            **evidence,
        }

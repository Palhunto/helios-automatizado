from __future__ import annotations

from typing import Protocol
from urllib.parse import urlparse

from ebook_pipeline.browser.urls import conversation_path_from_page_url
from ebook_pipeline.config import AppConfig
from ebook_pipeline.core.errors import ConfigurationError, IntegrityError
from ebook_pipeline.storage.database import Database
from ebook_pipeline.storage.repositories import ProjectRepository


class UserTurnProbeAdapter(Protocol):
    def user_turn_structural_probe(self, conversation_path: str) -> dict[str, object]: ...


class UserTurnProbeService:
    """Read-only orchestration for one existing conversation user-turn audit."""

    def __init__(
        self,
        config: AppConfig,
        database: Database,
        adapter: UserTurnProbeAdapter,
    ) -> None:
        self.config = config
        self.database = database
        self.adapter = adapter

    def run(self, project_id: str, conversation_url: str) -> dict[str, object]:
        if self.config.browser_channel != "chrome":
            raise ConfigurationError(
                "BROWSER_USER_TURN_SPIKE_REQUIRES_CHROME",
                "User-turn spike requires browser channel 'chrome'",
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
        with self.database.read_only_connection() as connection:
            project = ProjectRepository(connection).get(project_id)
        evidence = self.adapter.user_turn_structural_probe(conversation_path)
        return {
            "project_id": project.id,
            "conversation_url": conversation_url,
            "database_mode": "read_only",
            **evidence,
        }

from __future__ import annotations

from typing import Protocol

from ebook_pipeline.browser.fingerprints import strict_request_text, transport_fingerprint
from ebook_pipeline.browser.repositories import (
    BrowserConversationRepository,
    BrowserInteractionRepository,
)
from ebook_pipeline.browser.urls import is_real_conversation_path
from ebook_pipeline.config import AppConfig
from ebook_pipeline.core.errors import ConfigurationError, IntegrityError, NotFoundError
from ebook_pipeline.core.hashing import sha256_bytes
from ebook_pipeline.storage.artifacts import ArtifactStore
from ebook_pipeline.storage.database import Database
from ebook_pipeline.storage.repositories import ArtifactRepository, ProjectRepository


class ComposerTextProbeAdapter(Protocol):
    def composer_text_diagnostic(
        self, expected_text: str, conversation_path: str
    ) -> dict[str, object]: ...


class ComposerTextProbeService:
    """Read-only comparison of one prepared request with the currently filled composer."""

    def __init__(
        self,
        config: AppConfig,
        database: Database,
        store: ArtifactStore,
        adapter: ComposerTextProbeAdapter,
    ) -> None:
        self.config = config
        self.database = database
        self.store = store
        self.adapter = adapter

    def run(self, project_id: str, interaction_id: str) -> dict[str, object]:
        if self.config.browser_channel != "chrome":
            raise ConfigurationError(
                "BROWSER_COMPOSER_TEXT_SPIKE_REQUIRES_CHROME",
                "Composer text spike requires browser channel 'chrome'",
            )
        with self.database.read_only_connection() as connection:
            project = ProjectRepository(connection).get(project_id)
            interaction = BrowserInteractionRepository(connection).get(interaction_id)
            if interaction.project_id != project.id:
                raise NotFoundError(
                    "BROWSER_INTERACTION_NOT_FOUND",
                    "Browser interaction was not found in the requested project",
                )
            conversation = BrowserConversationRepository(connection).get(
                interaction.conversation_id
            )
            if (
                conversation.project_id != project.id
                or conversation.conversation_path is None
                or not is_real_conversation_path(conversation.conversation_path)
            ):
                raise IntegrityError(
                    "BROWSER_COMPOSER_TEXT_SPIKE_CONVERSATION_UNAVAILABLE",
                    "Interaction has no canonical conversation path for read-only inspection",
                )
            artifacts = ArtifactRepository(connection).list_for_project(project.id)
            artifact = next(
                (
                    candidate
                    for candidate in artifacts
                    if candidate.id == interaction.request_artifact_id
                ),
                None,
            )
            if artifact is None or artifact.sha256 != interaction.request_sha256:
                raise IntegrityError(
                    "BROWSER_REQUEST_ARTIFACT_INVALID",
                    "Interaction request artifact binding differs",
                )
            inspected = self.store.inspect(project.artifact_root, artifact.relative_path)
            if inspected.sha256 != artifact.sha256 or inspected.byte_size != artifact.byte_size:
                raise IntegrityError(
                    "BROWSER_REQUEST_ARTIFACT_INVALID",
                    "Interaction request artifact bytes differ",
                )
            content = self.store.resolve(
                project.artifact_root, artifact.relative_path
            ).read_bytes()
        if sha256_bytes(content) != interaction.request_sha256:
            raise IntegrityError(
                "BROWSER_REQUEST_ARTIFACT_INVALID",
                "Interaction request changed during read-only diagnostic preparation",
            )
        expected_text = strict_request_text(content)
        if transport_fingerprint(expected_text) != interaction.transport_fingerprint:
            raise IntegrityError(
                "BROWSER_TRANSPORT_FINGERPRINT_MISMATCH",
                "Interaction request no longer matches its transport fingerprint",
            )
        diagnostic = self.adapter.composer_text_diagnostic(
            expected_text, conversation.conversation_path
        )
        return {
            "project_id": project.id,
            "interaction_id": interaction.id,
            "interaction_kind": interaction.kind.value,
            "unit_id": interaction.unit_id,
            "request_artifact_id": artifact.id,
            "conversation_path": conversation.conversation_path,
            "database_mode": "read_only",
            **diagnostic,
        }

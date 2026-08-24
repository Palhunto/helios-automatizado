from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from typing import Protocol

from ebook_pipeline.browser.fingerprints import strict_request_text
from ebook_pipeline.browser.models import ComposerProbeCaseResult
from ebook_pipeline.config import AppConfig
from ebook_pipeline.core.errors import ConfigurationError, IntegrityError, NotFoundError
from ebook_pipeline.core.hashing import sha256_bytes
from ebook_pipeline.storage.artifacts import ArtifactStore
from ebook_pipeline.storage.database import Database
from ebook_pipeline.storage.repositories import ProjectRepository
from ebook_pipeline.writing.repositories import (
    WritingArtifactRepository,
    WritingContextRepository,
)

SHORT_PAYLOAD = "HELIOS-COMPOSER-PROBE-SHORT"
MULTILINE_UNICODE_PAYLOAD = (
    "HÉLIOS — composer probe\n"
    "Linha Unicode: ação, síntese, Ω, 漢字.\n"
    "Whitespace preservado: fim."
)
LARGE_PAYLOAD = "".join(
    f"HELIOS-COMPOSER-PROBE-LARGE-{index:04d}-á-Ω-漢字\n" for index in range(512)
)


class ComposerProbeAdapter(Protocol):
    def composer_probe(
        self, payloads: Mapping[str, str]
    ) -> dict[str, ComposerProbeCaseResult]: ...


class ComposerProbeService:
    """Read-only orchestration for a no-send headed composer diagnostic."""

    def __init__(
        self,
        config: AppConfig,
        database: Database,
        store: ArtifactStore,
        adapter: ComposerProbeAdapter,
    ) -> None:
        self.config = config
        self.database = database
        self.store = store
        self.adapter = adapter

    def run(
        self, project_id: str, request_artifact_id: str, *, artifact_only: bool = False
    ) -> dict[str, object]:
        if self.config.browser_channel != "chrome":
            raise ConfigurationError(
                "BROWSER_COMPOSER_SPIKE_REQUIRES_CHROME",
                "Composer spike requires browser channel 'chrome'",
            )
        context_id, request = self._read_context_request(project_id, request_artifact_id)
        payloads = (
            {"writing_context_request": request}
            if artifact_only
            else {
                "short": SHORT_PAYLOAD,
                "multiline_unicode": MULTILINE_UNICODE_PAYLOAD,
                "large": LARGE_PAYLOAD,
                "writing_context_request": request,
            }
        )
        cases = self.adapter.composer_probe(payloads)
        return {
            "project_id": project_id,
            "context_id": context_id,
            "request_artifact_id": request_artifact_id,
            "artifact_only": artifact_only,
            "database_mode": "read_only",
            "cases": {name: asdict(result) for name, result in cases.items()},
        }

    def _read_context_request(
        self, project_id: str, request_artifact_id: str
    ) -> tuple[str, str]:
        with self.database.read_only_connection() as connection:
            project = ProjectRepository(connection).get(project_id)
            artifact = WritingArtifactRepository(connection).get(request_artifact_id)
            if artifact.project_id != project.id:
                raise NotFoundError(
                    "WRITING_ARTIFACT_NOT_FOUND",
                    "Requested WritingContext artifact was not found for this project",
                )
            if artifact.artifact_type != "writing_context_package":
                raise IntegrityError(
                    "WRITING_CONTEXT_ARTIFACT_TYPE_INVALID",
                    "Requested artifact is not a WritingContext package",
                )
            row = connection.execute(
                "SELECT id FROM writing_contexts "
                "WHERE project_id = ? AND package_artifact_id = ?",
                (project.id, artifact.id),
            ).fetchone()
            if row is None:
                raise IntegrityError(
                    "WRITING_CONTEXT_ARTIFACT_UNBOUND",
                    "Requested WritingContext package is not bound to its context",
                )
            context = WritingContextRepository(connection).get(str(row["id"]))
            if artifact.sha256 != context.package_sha256:
                raise IntegrityError(
                    "WRITING_PROVENANCE_HASH_MISMATCH",
                    "WritingContext request artifact differs from context provenance",
                )
            inspected = self.store.inspect(project.artifact_root, artifact.relative_path)
            if inspected.sha256 != artifact.sha256 or inspected.byte_size != artifact.byte_size:
                raise IntegrityError(
                    "WRITING_ARTIFACT_HASH_MISMATCH",
                    "WritingContext request artifact differs from its database record",
                )
            content = self.store.resolve(
                project.artifact_root, artifact.relative_path
            ).read_bytes()
        if sha256_bytes(content) != context.package_sha256:
            raise IntegrityError(
                "WRITING_ARTIFACT_HASH_MISMATCH",
                "WritingContext request changed during read-only probe preparation",
            )
        return context.id, strict_request_text(content)

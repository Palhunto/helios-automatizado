from __future__ import annotations

import os
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path

from ebook_pipeline.core.errors import ArtifactError, ConflictError
from ebook_pipeline.core.hashing import sha256_bytes, sha256_file
from ebook_pipeline.core.ids import new_id
from ebook_pipeline.core.models import StoredFile
from ebook_pipeline.validation.paths import resolve_under, validate_relative_path

PROJECT_DIRECTORIES = (
    "config",
    "prompts/frozen",
    "academic/questionnaire",
    "academic/consolidated-answers",
    "academic/plan",
    "text/raw",
    "text/accepted",
    "text/references",
    "text/consolidated",
    "visual-plan/pagination",
    "visual-plan/raw",
    "visual-plan/validation",
    "visual-plan/accepted",
    "visual-plan/anchors",
    "images",
    "exports",
    "logs",
    "tmp",
)


class ArtifactStore:
    def __init__(self, projects_root: Path) -> None:
        self.projects_root = projects_root.resolve()

    def project_root(self, artifact_root: str) -> Path:
        return resolve_under(self.projects_root, artifact_root)

    def initialize_project(self, artifact_root: str) -> Path:
        root = self.project_root(artifact_root)
        root.mkdir(parents=True, exist_ok=True)
        for relative in PROJECT_DIRECTORIES:
            resolve_under(root, relative).mkdir(parents=True, exist_ok=True)
        return root

    def resolve(self, artifact_root: str, relative_path: str) -> Path:
        return resolve_under(self.project_root(artifact_root), relative_path)

    def write_bytes(
        self,
        artifact_root: str,
        relative_path: str,
        content: bytes,
        *,
        validator: Callable[[Path], None] | None = None,
    ) -> StoredFile:
        normalized = validate_relative_path(relative_path)
        destination = self.resolve(artifact_root, normalized)
        destination.parent.mkdir(parents=True, exist_ok=True)
        expected_hash = sha256_bytes(content)
        if destination.exists():
            return self._accept_existing(destination, normalized, expected_hash, len(content))

        temporary = destination.with_name(f".{destination.name}.{new_id()}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            if validator is not None:
                try:
                    validator(temporary)
                except Exception as exc:
                    raise ArtifactError(
                        "ARTIFACT_VALIDATION_FAILED",
                        f"Validation failed for {normalized!r}: {exc}",
                    ) from exc
            if sha256_file(temporary) != expected_hash:
                raise ArtifactError(
                    "ARTIFACT_TEMP_HASH_MISMATCH", f"Temporary write for {normalized!r} is corrupt"
                )
            try:
                os.link(temporary, destination)
            except FileExistsError:
                return self._accept_existing(destination, normalized, expected_hash, len(content))
            except OSError as exc:
                raise ArtifactError(
                    "ATOMIC_PROMOTION_FAILED",
                    f"Could not atomically promote {normalized!r}: {exc}",
                    recoverable=True,
                ) from exc
            return StoredFile(
                relative_path=normalized,
                sha256=expected_hash,
                byte_size=len(content),
                already_existed=False,
            )
        except (ArtifactError, ConflictError):
            raise
        except OSError as exc:
            raise ArtifactError(
                "ARTIFACT_WRITE_FAILED",
                f"Could not write {normalized!r}: {exc}",
                recoverable=True,
            ) from exc
        finally:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)

    def inspect(self, artifact_root: str, relative_path: str) -> StoredFile:
        normalized = validate_relative_path(relative_path)
        path = self.resolve(artifact_root, normalized)
        if not path.is_file():
            raise ArtifactError(
                "ARTIFACT_MISSING", f"Artifact {normalized!r} does not exist", recoverable=True
            )
        return StoredFile(
            relative_path=normalized,
            sha256=sha256_file(path),
            byte_size=path.stat().st_size,
            already_existed=True,
        )

    @staticmethod
    def _accept_existing(
        path: Path, relative_path: str, expected_hash: str, expected_size: int
    ) -> StoredFile:
        if not path.is_file():
            raise ConflictError(
                "ARTIFACT_DESTINATION_NOT_FILE", f"Destination {relative_path!r} is not a file"
            )
        actual_hash = sha256_file(path)
        actual_size = path.stat().st_size
        if actual_hash != expected_hash or actual_size != expected_size:
            raise ConflictError(
                "ARTIFACT_DESTINATION_CONFLICT",
                f"Destination {relative_path!r} already contains different bytes",
            )
        return StoredFile(
            relative_path=relative_path,
            sha256=actual_hash,
            byte_size=actual_size,
            already_existed=True,
        )

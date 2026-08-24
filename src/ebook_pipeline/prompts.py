from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ebook_pipeline.core.errors import ConfigurationError, IntegrityError, NotFoundError
from ebook_pipeline.core.hashing import sha256_bytes
from ebook_pipeline.validation.paths import resolve_under


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PromptEntry(_StrictModel):
    id: str = Field(min_length=1)
    version: int = Field(ge=1)
    status: str = Field(min_length=1)
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    domain: str = Field(min_length=1)


class PromptRegistryModel(_StrictModel):
    prompts: list[PromptEntry] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_versions(self) -> PromptRegistryModel:
        identities = [(item.id, item.version) for item in self.prompts]
        if len(identities) != len(set(identities)):
            raise ValueError("prompt id/version pairs must be unique")
        return self


@dataclass(frozen=True, slots=True)
class ResolvedPrompt:
    id: str
    version: int
    sha256: str
    path: Path
    content: bytes


class PromptRegistry:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()

    def _load(self) -> PromptRegistryModel:
        try:
            raw = self.path.read_bytes()
        except OSError as exc:
            raise ConfigurationError(
                "PROMPT_REGISTRY_READ_ERROR",
                f"Could not read prompt registry {self.path}: {exc}",
            ) from exc
        try:
            value = yaml.safe_load(raw)
            return PromptRegistryModel.model_validate(value)
        except (yaml.YAMLError, ValidationError) as exc:
            raise ConfigurationError(
                "PROMPT_REGISTRY_INVALID", f"Invalid prompt registry: {exc}"
            ) from exc

    def resolve(self, prompt_id: str, version: int) -> ResolvedPrompt:
        registry = self._load()
        entry = next(
            (item for item in registry.prompts if item.id == prompt_id and item.version == version),
            None,
        )
        if entry is None:
            raise NotFoundError(
                "PROMPT_NOT_FOUND", f"Prompt {prompt_id}@{version} is not registered"
            )
        path = resolve_under(self.path.parent, entry.path)
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise IntegrityError(
                "PROMPT_FILE_MISSING", f"Could not read registered prompt {path}: {exc}"
            ) from exc
        digest = sha256_bytes(content)
        if digest != entry.sha256:
            raise IntegrityError(
                "PROMPT_HASH_MISMATCH",
                f"Prompt {prompt_id}@{version} differs from its registry hash",
                evidence={"expected": entry.sha256, "actual": digest},
            )
        return ResolvedPrompt(entry.id, entry.version, digest, path, content)

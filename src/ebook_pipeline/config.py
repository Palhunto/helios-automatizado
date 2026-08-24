from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from ebook_pipeline.core.errors import ConfigurationError
from ebook_pipeline.core.hashing import canonical_hash

SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ProjectIdentityConfig(StrictModel):
    name: str = Field(min_length=1)
    slug: str

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name must not be blank")
        return value.strip()

    @field_validator("slug")
    @classmethod
    def validate_slug(cls, value: str) -> str:
        if len(value) > 80 or not SLUG_PATTERN.fullmatch(value):
            raise ValueError("slug must use lowercase ASCII words separated by single hyphens")
        return value


class EditorialConfig(StrictModel):
    discipline: str = Field(min_length=1)
    desired_structure: str = Field(min_length=1)
    additional_materials: list[str] = Field(default_factory=list)


class PromptReference(StrictModel):
    id: str = Field(min_length=1)
    version: int = Field(ge=1)


class PromptSelections(StrictModel):
    academic_planning: PromptReference
    writing: PromptReference
    visual_planning: PromptReference
    image_global_style: PromptReference


class OutputConfig(StrictModel):
    google_docs_id: str | None = None


class ProjectRuntimeConfig(StrictModel):
    language: str = Field(min_length=2)
    allow_api_fallback: bool
    browser_automation_enabled: bool


class ProjectConfig(StrictModel):
    project: ProjectIdentityConfig
    editorial: EditorialConfig
    prompts: PromptSelections
    outputs: OutputConfig = Field(default_factory=OutputConfig)
    runtime: ProjectRuntimeConfig


class PipelineStage(StrictModel):
    id: str = Field(min_length=1)
    depends_on: list[str] = Field(default_factory=list)
    requires_event: str | None = None
    units: list[str] = Field(default_factory=list)

    @field_validator("depends_on", "units")
    @classmethod
    def unique_values(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("values must be unique")
        return values


class PipelineConfig(StrictModel):
    stages: list[PipelineStage] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_graph(self) -> PipelineConfig:
        ids = [stage.id for stage in self.stages]
        if len(ids) != len(set(ids)):
            raise ValueError("stage ids must be unique")
        known = set(ids)
        graph: dict[str, list[str]] = {}
        for stage in self.stages:
            missing = set(stage.depends_on) - known
            if missing:
                raise ValueError(f"stage {stage.id!r} has unknown dependencies: {sorted(missing)}")
            if stage.id in stage.depends_on:
                raise ValueError(f"stage {stage.id!r} cannot depend on itself")
            graph[stage.id] = stage.depends_on

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(stage_id: str) -> None:
            if stage_id in visiting:
                raise ValueError(f"pipeline dependency cycle includes {stage_id!r}")
            if stage_id in visited:
                return
            visiting.add(stage_id)
            for dependency in graph[stage_id]:
                visit(dependency)
            visiting.remove(stage_id)
            visited.add(stage_id)

        for stage_id in ids:
            visit(stage_id)
        return self


class AppConfig(StrictModel):
    data_dir: Path
    projects_dir: Path
    pipeline_config: Path
    prompt_registry: Path = Path("./prompts/registry.yaml")
    writing_contract_registry: Path = Path("./writing_contracts/registry.yaml")
    log_level: str = "INFO"
    max_attempts: int = Field(default=3, ge=1)
    browser_channel: Literal["chrome", "chromium"] = "chrome"
    browser_profile_dir: Path = Path("./data/browser-profile")
    browser_headless: bool = False
    browser_timeout_seconds: int = Field(default=180, ge=10)
    browser_base_url: str = "https://chatgpt.com"
    browser_capture_method_version: str = "rendered_text_v1"

    ALLOWED_LOG_LEVELS: ClassVar[set[str]] = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, value: str) -> str:
        normalized = value.upper()
        if normalized not in cls.ALLOWED_LOG_LEVELS:
            raise ValueError(f"unsupported log level: {value}")
        return normalized

    @field_validator("browser_capture_method_version")
    @classmethod
    def validate_capture_method(cls, value: str) -> str:
        if value not in {"rendered_text_v1", "copy_text_v1"}:
            raise ValueError("unsupported browser capture method version")
        return value

    @field_validator("browser_base_url")
    @classmethod
    def validate_browser_base_url(cls, value: str) -> str:
        if value.rstrip("/") != "https://chatgpt.com":
            raise ValueError("browser_base_url must be https://chatgpt.com")
        return value.rstrip("/")

    @property
    def database_path(self) -> Path:
        return self.data_dir / "helios.db"


@dataclass(frozen=True, slots=True)
class LoadedProjectConfig:
    model: ProjectConfig
    raw_bytes: bytes
    input_hash: str


def _load_yaml(path: Path) -> tuple[dict[str, Any], bytes]:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ConfigurationError(
            "CONFIG_READ_ERROR", f"Could not read configuration {path}: {exc}"
        ) from exc
    try:
        value = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigurationError("CONFIG_YAML_INVALID", f"Invalid YAML in {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ConfigurationError("CONFIG_ROOT_INVALID", f"Configuration {path} must be a mapping")
    return value, raw


def load_project_config(path: Path) -> LoadedProjectConfig:
    mapping, raw = _load_yaml(path)
    try:
        model = ProjectConfig.model_validate(mapping)
    except ValidationError as exc:
        raise ConfigurationError("PROJECT_CONFIG_INVALID", str(exc)) from exc
    return LoadedProjectConfig(
        model=model,
        raw_bytes=raw,
        input_hash=canonical_hash(model.model_dump(mode="json")),
    )


def load_pipeline_config(path: Path) -> PipelineConfig:
    try:
        mapping, _ = _load_yaml(path)
        return PipelineConfig.model_validate(mapping)
    except ValidationError as exc:
        raise ConfigurationError("PIPELINE_CONFIG_INVALID", str(exc)) from exc


def load_app_config(overrides: dict[str, object] | None = None) -> AppConfig:
    overrides = {key: value for key, value in (overrides or {}).items() if value is not None}
    try:
        environment_max_attempts = int(os.environ.get("HELIOS_MAX_ATTEMPTS", "3"))
    except ValueError as exc:
        raise ConfigurationError(
            "APP_CONFIG_INVALID", "HELIOS_MAX_ATTEMPTS must be an integer"
        ) from exc
    local_app_data = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
    browser_channel = str(
        overrides.get("browser_channel", os.environ.get("HELIOS_BROWSER_CHANNEL", "chrome"))
    )
    default_profile_name = (
        "chrome-profile" if browser_channel == "chrome" else "chatgpt-profile"
    )
    headless_value = os.environ.get("HELIOS_BROWSER_HEADLESS", "false").casefold()
    if headless_value not in {"true", "false", "1", "0"}:
        raise ConfigurationError(
            "APP_CONFIG_INVALID", "HELIOS_BROWSER_HEADLESS must be true or false"
        )
    try:
        browser_timeout_seconds = int(os.environ.get("HELIOS_BROWSER_TIMEOUT_SECONDS", "180"))
    except ValueError as exc:
        raise ConfigurationError(
            "APP_CONFIG_INVALID", "HELIOS_BROWSER_TIMEOUT_SECONDS must be an integer"
        ) from exc
    values: dict[str, object] = {
        "data_dir": Path(os.environ.get("HELIOS_DATA_DIR", "./data")),
        "projects_dir": Path(os.environ.get("HELIOS_PROJECTS_DIR", "./projects")),
        "pipeline_config": Path(os.environ.get("HELIOS_PIPELINE_CONFIG", "./config/pipeline.yaml")),
        "prompt_registry": Path(
            os.environ.get("HELIOS_PROMPT_REGISTRY", "./prompts/registry.yaml")
        ),
        "writing_contract_registry": Path(
            os.environ.get("HELIOS_WRITING_CONTRACT_REGISTRY", "./writing_contracts/registry.yaml")
        ),
        "log_level": os.environ.get("HELIOS_LOG_LEVEL", "INFO"),
        "max_attempts": environment_max_attempts,
        "browser_channel": browser_channel,
        "browser_profile_dir": Path(
            os.environ.get(
                "HELIOS_BROWSER_PROFILE_DIR",
                str(local_app_data / "HeliosEbookAutomation" / default_profile_name),
            )
        ),
        "browser_headless": headless_value in {"true", "1"},
        "browser_timeout_seconds": browser_timeout_seconds,
        "browser_base_url": os.environ.get("HELIOS_BROWSER_BASE_URL", "https://chatgpt.com"),
        "browser_capture_method_version": os.environ.get(
            "HELIOS_BROWSER_CAPTURE_METHOD_VERSION", "rendered_text_v1"
        ),
    }
    values.update(overrides)
    try:
        config = AppConfig.model_validate(values)
    except (ValidationError, ValueError) as exc:
        raise ConfigurationError("APP_CONFIG_INVALID", str(exc)) from exc
    resolved = config.model_copy(
        update={
            "data_dir": config.data_dir.resolve(),
            "projects_dir": config.projects_dir.resolve(),
            "pipeline_config": config.pipeline_config.resolve(),
            "prompt_registry": config.prompt_registry.resolve(),
            "writing_contract_registry": config.writing_contract_registry.resolve(),
            "browser_profile_dir": config.browser_profile_dir.resolve(),
        }
    )
    if resolved.browser_profile_dir.is_relative_to(resolved.projects_dir):
        raise ConfigurationError(
            "APP_CONFIG_INVALID", "Browser profile must stay outside the projects directory"
        )
    return resolved

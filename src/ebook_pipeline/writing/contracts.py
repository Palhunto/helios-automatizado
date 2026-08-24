from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from ebook_pipeline.core.errors import ConfigurationError, IntegrityError, NotFoundError
from ebook_pipeline.core.hashing import sha256_bytes
from ebook_pipeline.validation.paths import resolve_under


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class LinguisticWarningPolicy(_StrictModel):
    repeated_first_word: bool = True
    repeated_initial: bool = True
    repeated_article: bool = True
    initial_run_length: int = Field(default=3, ge=2)
    articles: list[str] = Field(default_factory=lambda: ["o", "a", "os", "as", "um", "uma"])


class ValidationDefaults(_StrictModel):
    allow_lists: bool = False
    forbidden_phrases: list[str] = Field(default_factory=list)
    linguistic_warnings: LinguisticWarningPolicy = Field(default_factory=LinguisticWarningPolicy)


class CharacterRange(_StrictModel):
    min: int = Field(ge=1)
    max: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_order(self) -> CharacterRange:
        if self.min > self.max:
            raise ValueError("character range min must be less than or equal to max")
        return self


class CharacterCountContract(_StrictModel):
    target: CharacterRange
    accepted: CharacterRange

    @model_validator(mode="after")
    def validate_accepted_encloses_target(self) -> CharacterCountContract:
        if self.accepted.min > self.target.min:
            raise ValueError("accepted.min must be less than or equal to target.min")
        if self.target.max > self.accepted.max:
            raise ValueError("target.max must be less than or equal to accepted.max")
        return self


class AcademicSectionSelector(_StrictModel):
    heading_pattern: str = Field(min_length=1)
    heading_level: int | None = Field(default=None, ge=1, le=6)
    ancestor_pattern: str | None = None
    required: bool = True


class AcademicContextSelector(_StrictModel):
    projection_version: int = Field(ge=1)
    sections: list[AcademicSectionSelector] = Field(min_length=1)


class WritingUnitContract(_StrictModel):
    unit_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_-]+$")
    order: int = Field(ge=1)
    character_count: CharacterCountContract | None = None
    min_characters: int | None = Field(default=None, ge=1)
    max_characters: int | None = Field(default=None, ge=1)
    continuity_from: list[str] = Field(default_factory=list)
    required_headings: list[str] = Field(default_factory=list)
    forbidden_headings: list[str] = Field(default_factory=list)
    final_heading: str | None = None
    allow_lists: bool | None = None
    academic_context: AcademicContextSelector | None = None

    @field_validator("continuity_from", "required_headings", "forbidden_headings")
    @classmethod
    def unique_values(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("contract lists must not contain duplicates")
        return values

    @model_validator(mode="after")
    def validate_limits_and_headings(self) -> WritingUnitContract:
        has_legacy_minimum = self.min_characters is not None
        has_legacy_maximum = self.max_characters is not None
        if self.character_count is None:
            if has_legacy_minimum != has_legacy_maximum:
                raise ValueError(
                    "legacy character limits require min_characters and max_characters"
                )
            if not has_legacy_minimum:
                raise ValueError("unit requires character_count or legacy character limits")
            if self.max_characters is None or self.min_characters is None:
                raise ValueError("legacy character limits are incomplete")
            if self.max_characters < self.min_characters:
                raise ValueError("max_characters must be greater than or equal to min_characters")
        elif has_legacy_minimum or has_legacy_maximum:
            raise ValueError("character_count cannot be combined with legacy character limits")
        overlap = set(self.required_headings) & set(self.forbidden_headings)
        if overlap:
            raise ValueError(f"headings cannot be both required and forbidden: {sorted(overlap)}")
        if self.final_heading is not None and self.final_heading not in self.required_headings:
            raise ValueError("final_heading must also be declared in required_headings")
        return self

    @property
    def target_character_range(self) -> CharacterRange:
        if self.character_count is not None:
            return self.character_count.target
        if self.min_characters is not None and self.max_characters is not None:
            return CharacterRange(min=self.min_characters, max=self.max_characters)
        raise IntegrityError(
            "WRITING_CONTRACT_INVALID", "Writing unit has no target character range"
        )

    @property
    def accepted_character_range(self) -> CharacterRange:
        if self.character_count is not None:
            return self.character_count.accepted
        if self.min_characters is not None and self.max_characters is not None:
            return CharacterRange(min=self.min_characters, max=self.max_characters)
        raise IntegrityError(
            "WRITING_CONTRACT_INVALID", "Writing unit has no accepted character range"
        )


class WritingContract(_StrictModel):
    id: str = Field(min_length=1)
    version: int = Field(ge=1)
    separator: str
    validation: ValidationDefaults = Field(default_factory=ValidationDefaults)
    units: list[WritingUnitContract] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_graph(self) -> WritingContract:
        identities = [unit.unit_id for unit in self.units]
        orders = [unit.order for unit in self.units]
        if len(identities) != len(set(identities)):
            raise ValueError("unit_id values must be unique")
        if len(orders) != len(set(orders)):
            raise ValueError("unit order values must be unique")
        known = set(identities)
        graph = {unit.unit_id: unit.continuity_from for unit in self.units}
        for unit in self.units:
            missing = set(unit.continuity_from) - known
            if missing:
                raise ValueError(
                    f"unit {unit.unit_id!r} has unknown dependencies: {sorted(missing)}"
                )
            if unit.unit_id in unit.continuity_from:
                raise ValueError(f"unit {unit.unit_id!r} cannot depend on itself")

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(unit_id: str) -> None:
            if unit_id in visiting:
                raise ValueError(f"writing dependency cycle includes {unit_id!r}")
            if unit_id in visited:
                return
            visiting.add(unit_id)
            for dependency in graph[unit_id]:
                visit(dependency)
            visiting.remove(unit_id)
            visited.add(unit_id)

        for unit_id in identities:
            visit(unit_id)
        return self

    def unit(self, unit_id: str) -> WritingUnitContract:
        unit = next((item for item in self.units if item.unit_id == unit_id), None)
        if unit is None:
            raise NotFoundError(
                "WRITING_UNIT_NOT_IN_CONTRACT",
                f"Writing unit {unit_id!r} is not declared by {self.id}@{self.version}",
            )
        return unit

    def ordered_units(self) -> tuple[WritingUnitContract, ...]:
        return tuple(sorted(self.units, key=lambda item: item.order))

    def topological_units(self) -> tuple[WritingUnitContract, ...]:
        ordered = self.ordered_units()
        remaining = {unit.unit_id: unit for unit in ordered}
        emitted: set[str] = set()
        result: list[WritingUnitContract] = []
        while remaining:
            available = [
                unit
                for unit in ordered
                if unit.unit_id in remaining and set(unit.continuity_from).issubset(emitted)
            ]
            if not available:
                raise IntegrityError(
                    "WRITING_CONTRACT_DAG_INVALID", "Writing contract has no topological order"
                )
            for unit in available:
                result.append(unit)
                emitted.add(unit.unit_id)
                del remaining[unit.unit_id]
        return tuple(result)


class ContractEntry(_StrictModel):
    id: str = Field(min_length=1)
    version: int = Field(ge=1)
    status: str = Field(min_length=1)
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ContractRegistryModel(_StrictModel):
    contracts: list[ContractEntry] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_versions(self) -> ContractRegistryModel:
        identities = [(item.id, item.version) for item in self.contracts]
        if len(identities) != len(set(identities)):
            raise ValueError("writing contract id/version pairs must be unique")
        return self


@dataclass(frozen=True, slots=True)
class ResolvedWritingContract:
    id: str
    version: int
    sha256: str
    path: Path
    content: bytes
    model: WritingContract


class WritingContractRegistry:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()

    def _load(self) -> ContractRegistryModel:
        try:
            value = yaml.safe_load(self.path.read_bytes())
            return ContractRegistryModel.model_validate(value)
        except OSError as exc:
            raise ConfigurationError(
                "WRITING_CONTRACT_REGISTRY_READ_ERROR",
                f"Could not read writing contract registry {self.path}: {exc}",
            ) from exc
        except (yaml.YAMLError, ValidationError) as exc:
            raise ConfigurationError(
                "WRITING_CONTRACT_REGISTRY_INVALID",
                f"Invalid writing contract registry: {exc}",
            ) from exc

    def resolve(self, contract_id: str, version: int) -> ResolvedWritingContract:
        entry = next(
            (
                item
                for item in self._load().contracts
                if item.id == contract_id and item.version == version
            ),
            None,
        )
        if entry is None:
            raise NotFoundError(
                "WRITING_CONTRACT_NOT_FOUND",
                f"Writing contract {contract_id}@{version} is not registered",
            )
        path = resolve_under(self.path.parent, entry.path)
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise IntegrityError(
                "WRITING_CONTRACT_FILE_MISSING",
                f"Could not read registered writing contract {path}: {exc}",
            ) from exc
        digest = sha256_bytes(content)
        if digest != entry.sha256:
            raise IntegrityError(
                "WRITING_CONTRACT_HASH_MISMATCH",
                f"Writing contract {contract_id}@{version} differs from its registry hash",
                evidence={"expected": entry.sha256, "actual": digest},
            )
        try:
            model = WritingContract.model_validate(yaml.safe_load(content))
        except (yaml.YAMLError, ValidationError) as exc:
            raise IntegrityError(
                "WRITING_CONTRACT_INVALID", f"Invalid writing contract: {exc}"
            ) from exc
        if model.id != entry.id or model.version != entry.version:
            raise IntegrityError(
                "WRITING_CONTRACT_IDENTITY_MISMATCH",
                "Writing contract content does not match its registry identity",
            )
        return ResolvedWritingContract(entry.id, entry.version, digest, path, content, model)

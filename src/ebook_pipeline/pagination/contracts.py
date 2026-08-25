from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from ebook_pipeline.core.errors import ConfigurationError, IntegrityError, NotFoundError
from ebook_pipeline.core.hashing import sha256_bytes, sha256_file
from ebook_pipeline.validation.paths import resolve_under


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class BoxMM(_StrictModel):
    x_mm: float = Field(ge=0)
    y_mm: float = Field(ge=0)
    width_mm: float = Field(gt=0)
    height_mm: float = Field(gt=0)


class PageContract(_StrictModel):
    format: Literal["A4"]
    orientation: Literal["portrait"]
    width_mm: float = Field(gt=0)
    height_mm: float = Field(gt=0)
    print_margin_mm: float = Field(ge=0)
    header: BoxMM
    footer: BoxMM
    eligible_text: BoxMM
    noneligible_text: BoxMM
    visual_slot: BoxMM
    visual_media_max_height_mm: float = Field(gt=0)
    visual_caption_height_mm: float = Field(gt=0)
    visual_gap_mm: float = Field(ge=0)

    @model_validator(mode="after")
    def validate_a4_and_regions(self) -> PageContract:
        if (self.width_mm, self.height_mm) != (210.0, 297.0):
            raise ValueError("A4 portrait must be exactly 210 x 297 mm")
        for name in ("header", "footer", "eligible_text", "noneligible_text", "visual_slot"):
            box = getattr(self, name)
            if box.x_mm + box.width_mm > self.width_mm:
                raise ValueError(f"{name} exceeds page width")
            if box.y_mm + box.height_mm > self.height_mm:
                raise ValueError(f"{name} exceeds page height")
        if self.eligible_text.y_mm + self.eligible_text.height_mm + self.visual_gap_mm > (
            self.visual_slot.y_mm
        ):
            raise ValueError("eligible text overlaps the visual slot or its gap")
        if (
            self.visual_media_max_height_mm + self.visual_caption_height_mm
            > self.visual_slot.height_mm
        ):
            raise ValueError("visual media and caption exceed the visual slot")
        return self


class TextStyle(_StrictModel):
    font_role: Literal["sans", "serif"]
    font_size_pt: float = Field(gt=0)
    line_height: float = Field(gt=0)
    font_weight: int = Field(ge=100, le=900)


class TypographyContract(_StrictModel):
    body: TextStyle
    h1: TextStyle
    h2: TextStyle
    h3: TextStyle
    paragraph_gap_mm: float = Field(ge=0)
    text_align: Literal["left"]
    hyphens: Literal["none"]
    orphans: int = Field(ge=2)
    widows: int = Field(ge=2)


class ChapterBoundaryContract(_StrictModel):
    chapter_units_start_new_page: Literal[True]
    inject_visible_heading: Literal[False]
    visible_heading_policy: Literal["source_only"]


class HeadingRule(_StrictModel):
    exact_text: str = Field(min_length=1)
    level: int = Field(ge=1, le=3)


class FontAssetContract(_StrictModel):
    role: Literal["sans", "serif"]
    family: str = Field(min_length=1)
    style: Literal["normal"]
    weight_min: int = Field(ge=100, le=900)
    weight_max: int = Field(ge=100, le=900)
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    license_path: str = Field(min_length=1)
    license_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_weights(self) -> FontAssetContract:
        if self.weight_min > self.weight_max:
            raise ValueError("font weight_min must not exceed weight_max")
        return self


class RendererContract(_StrictModel):
    engine: Literal["chromium_explicit_dom_v1"]
    paginator_version: Literal[1]
    template_version: Literal[1]
    pdf_canonicalization_version: Literal[1]
    measurement_epsilon_px: float = Field(ge=0, le=1)


class UnitLayoutContract(_StrictModel):
    unit_id: str = Field(min_length=1, pattern=r"^[A-Z0-9_]+$")
    order: int = Field(ge=1)
    section: Literal["introduction", "chapter", "conclusion"]
    chapter_id: str | None = Field(default=None, pattern=r"^CH0[1-8]$")
    eligible: bool
    starts_new_page: bool

    @model_validator(mode="after")
    def validate_mapping(self) -> UnitLayoutContract:
        is_chapter = self.section == "chapter"
        if is_chapter != (self.chapter_id is not None):
            raise ValueError("chapter_id is required only for chapter units")
        if self.eligible != is_chapter:
            raise ValueError("only chapter units are eligible")
        return self


class PaginationLayout(_StrictModel):
    schema_: Literal["helios_pagination_layout@1"] = Field(alias="schema")
    id: Literal["helios_pagination_layout"]
    version: Literal[1]
    page: PageContract
    typography: TypographyContract
    chapter_boundary: ChapterBoundaryContract
    heading_rules: list[HeadingRule]
    fonts: list[FontAssetContract] = Field(min_length=2)
    renderer: RendererContract
    units: list[UnitLayoutContract] = Field(min_length=1)

    @field_validator("heading_rules")
    @classmethod
    def unique_headings(cls, values: list[HeadingRule]) -> list[HeadingRule]:
        texts = [item.exact_text for item in values]
        if len(texts) != len(set(texts)):
            raise ValueError("heading rules must be unique")
        return values

    @model_validator(mode="after")
    def validate_units_and_fonts(self) -> PaginationLayout:
        unit_ids = [item.unit_id for item in self.units]
        orders = [item.order for item in self.units]
        if len(unit_ids) != len(set(unit_ids)) or len(orders) != len(set(orders)):
            raise ValueError("layout unit ids and orders must be unique")
        if sorted(orders) != list(range(1, len(orders) + 1)):
            raise ValueError("layout unit orders must be contiguous")
        roles = [item.role for item in self.fonts]
        if sorted(roles) != ["sans", "serif"]:
            raise ValueError("layout requires exactly one sans and one serif font asset")
        for chapter_number in range(1, 9):
            chapter_id = f"CH{chapter_number:02d}"
            mapped = [item for item in self.units if item.chapter_id == chapter_id]
            if (
                len(mapped) != 2
                or mapped[0].unit_id != f"{chapter_id}_A"
                or mapped[1].unit_id != f"{chapter_id}_B"
            ):
                raise ValueError(f"{chapter_id} must map exactly its A and B units")
            if not mapped[0].starts_new_page or mapped[1].starts_new_page:
                raise ValueError(f"{chapter_id} page boundary must be structural at its A unit")
        return self

    def ordered_units(self) -> tuple[UnitLayoutContract, ...]:
        return tuple(sorted(self.units, key=lambda item: item.order))


class LayoutEntry(_StrictModel):
    id: str = Field(min_length=1)
    version: int = Field(ge=1)
    status: str = Field(min_length=1)
    path: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class LayoutRegistryModel(_StrictModel):
    layouts: list[LayoutEntry] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_versions(self) -> LayoutRegistryModel:
        identities = [(item.id, item.version) for item in self.layouts]
        if len(identities) != len(set(identities)):
            raise ValueError("pagination layout id/version pairs must be unique")
        return self


@dataclass(frozen=True, slots=True)
class ResolvedFontAsset:
    contract: FontAssetContract
    path: Path
    license_path: Path


@dataclass(frozen=True, slots=True)
class ResolvedPaginationLayout:
    id: str
    version: int
    sha256: str
    path: Path
    content: bytes
    model: PaginationLayout
    fonts: tuple[ResolvedFontAsset, ...]


class PaginationLayoutRegistry:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()

    def _load(self) -> LayoutRegistryModel:
        try:
            return LayoutRegistryModel.model_validate(yaml.safe_load(self.path.read_bytes()))
        except OSError as exc:
            raise ConfigurationError(
                "PAGINATION_LAYOUT_REGISTRY_READ_ERROR",
                f"Could not read pagination layout registry {self.path}: {exc}",
            ) from exc
        except (yaml.YAMLError, ValidationError) as exc:
            raise ConfigurationError(
                "PAGINATION_LAYOUT_REGISTRY_INVALID",
                f"Invalid pagination layout registry: {exc}",
            ) from exc

    def resolve(self, layout_id: str, version: int) -> ResolvedPaginationLayout:
        entry = next(
            (
                item
                for item in self._load().layouts
                if item.id == layout_id and item.version == version
            ),
            None,
        )
        if entry is None:
            raise NotFoundError(
                "PAGINATION_LAYOUT_NOT_FOUND",
                f"Pagination layout {layout_id}@{version} is not registered",
            )
        path = resolve_under(self.path.parent, entry.path)
        try:
            content = path.read_bytes()
        except OSError as exc:
            raise IntegrityError(
                "PAGINATION_LAYOUT_FILE_MISSING",
                f"Could not read registered pagination layout {path}: {exc}",
            ) from exc
        digest = sha256_bytes(content)
        if digest != entry.sha256:
            raise IntegrityError(
                "PAGINATION_LAYOUT_HASH_MISMATCH",
                f"Pagination layout {layout_id}@{version} differs from its registry hash",
                evidence={"actual": digest, "expected": entry.sha256},
            )
        try:
            model = PaginationLayout.model_validate(yaml.safe_load(content))
        except (yaml.YAMLError, ValidationError) as exc:
            raise IntegrityError("PAGINATION_LAYOUT_INVALID", str(exc)) from exc
        if model.id != entry.id or model.version != entry.version:
            raise IntegrityError(
                "PAGINATION_LAYOUT_IDENTITY_MISMATCH",
                "Pagination layout content does not match its registry identity",
            )
        fonts: list[ResolvedFontAsset] = []
        for font in model.fonts:
            font_path = resolve_under(path.parent, font.path)
            license_path = resolve_under(path.parent, font.license_path)
            if not font_path.is_file() or not license_path.is_file():
                raise IntegrityError(
                    "PAGINATION_FONT_ASSET_MISSING",
                    f"Required licensed font asset for role {font.role!r} is missing",
                )
            if (
                sha256_file(font_path) != font.sha256
                or sha256_file(license_path) != font.license_sha256
            ):
                raise IntegrityError(
                    "PAGINATION_FONT_ASSET_HASH_MISMATCH",
                    "Required font or license for "
                    f"role {font.role!r} differs from the layout contract",
                )
            fonts.append(ResolvedFontAsset(font, font_path, license_path))
        return ResolvedPaginationLayout(
            entry.id,
            entry.version,
            digest,
            path,
            content,
            model,
            tuple(fonts),
        )

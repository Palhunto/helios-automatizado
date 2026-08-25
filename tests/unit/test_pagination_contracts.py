import json
from pathlib import Path

import pytest

from ebook_pipeline.core.errors import IntegrityError
from ebook_pipeline.core.hashing import sha256_bytes
from ebook_pipeline.pagination.contracts import PaginationLayoutRegistry


def test_pagination_json_schemas_are_strict_nested_contracts(repository_root: Path) -> None:
    layout_schema = json.loads(
        (repository_root / "schemas" / "pagination_layout.schema.json").read_text(
            encoding="utf-8"
        )
    )
    snapshot_schema = json.loads(
        (repository_root / "schemas" / "pagination_snapshot.schema.json").read_text(
            encoding="utf-8"
        )
    )

    assert layout_schema["properties"]["page"]["additionalProperties"] is False
    assert layout_schema["properties"]["chapter_boundary"]["properties"][
        "inject_visible_heading"
    ] == {"const": False}
    assert snapshot_schema["properties"]["renderer"]["additionalProperties"] is False
    assert snapshot_schema["$defs"]["page"]["additionalProperties"] is False


def test_canonical_layout_resolves_with_pinned_licensed_fonts(repository_root: Path) -> None:
    resolved = PaginationLayoutRegistry(
        repository_root / "pagination_layouts" / "registry.yaml"
    ).resolve("helios_pagination_layout", 1)

    assert resolved.sha256 == (
        "206e15338dd708c12665c23b36f38b28efb2a6df67f239f6876ae52325e03390"
    )
    assert resolved.model.schema_ == "helios_pagination_layout@1"
    assert resolved.model.chapter_boundary.inject_visible_heading is False
    assert resolved.model.chapter_boundary.visible_heading_policy == "source_only"
    assert [font.contract.role for font in resolved.fonts] == ["sans", "serif"]
    assert len(resolved.model.units) == 18
    assert all(item.eligible for item in resolved.model.units if item.chapter_id is not None)


def test_layout_registry_rejects_font_hash_drift(tmp_path: Path, repository_root: Path) -> None:
    source_root = repository_root / "pagination_layouts"
    layout = (source_root / "helios_pagination_layout_v1.yaml").read_bytes()
    layout_path = tmp_path / "layout.yaml"
    layout_path.write_bytes(layout)
    assets = tmp_path / "assets" / "fonts"
    assets.mkdir(parents=True)
    for name in ("NotoSans-OFL.txt", "NotoSerif-OFL.txt", "NotoSerif-wdth-wght.ttf"):
        (assets / name).write_bytes((source_root / "assets" / "fonts" / name).read_bytes())
    (assets / "NotoSans-wdth-wght.ttf").write_bytes(b"not the pinned font")
    registry = tmp_path / "registry.yaml"
    registry.write_text(
        "layouts:\n"
        "  - id: helios_pagination_layout\n"
        "    version: 1\n"
        "    status: active\n"
        "    path: layout.yaml\n"
        f"    sha256: {sha256_bytes(layout)}\n",
        encoding="utf-8",
    )

    with pytest.raises(IntegrityError) as captured:
        PaginationLayoutRegistry(registry).resolve("helios_pagination_layout", 1)
    assert captured.value.code == "PAGINATION_FONT_ASSET_HASH_MISMATCH"


def test_layout_registry_rejects_missing_font_without_system_fallback(
    tmp_path: Path, repository_root: Path
) -> None:
    source_root = repository_root / "pagination_layouts"
    layout = (source_root / "helios_pagination_layout_v1.yaml").read_bytes()
    (tmp_path / "layout.yaml").write_bytes(layout)
    registry = tmp_path / "registry.yaml"
    registry.write_text(
        "layouts:\n"
        "  - id: helios_pagination_layout\n"
        "    version: 1\n"
        "    status: active\n"
        "    path: layout.yaml\n"
        f"    sha256: {sha256_bytes(layout)}\n",
        encoding="utf-8",
    )

    with pytest.raises(IntegrityError) as captured:
        PaginationLayoutRegistry(registry).resolve("helios_pagination_layout", 1)
    assert captured.value.code == "PAGINATION_FONT_ASSET_MISSING"

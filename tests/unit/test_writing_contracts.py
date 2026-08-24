from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from ebook_pipeline.core.errors import ConfigurationError, IntegrityError, NotFoundError
from ebook_pipeline.core.hashing import sha256_bytes
from ebook_pipeline.writing.contracts import WritingContract, WritingContractRegistry


def test_real_and_alternate_contracts_resolve_without_engine_special_cases(
    repository_root: Path,
) -> None:
    registry = WritingContractRegistry(repository_root / "writing_contracts" / "registry.yaml")
    canonical = registry.resolve("omega_writing_production", 1)
    assert len(canonical.model.units) == 18
    assert canonical.model.ordered_units()[0].unit_id == "INTRO"
    assert canonical.model.ordered_units()[-1].unit_id == "CONCLUSION"
    assert canonical.model.unit("CH01_B").final_heading == "Exercícios analíticos e aplicados"
    assert canonical.sha256 == "9a247d93bcd00c5afa6e41d92ca9d83a5e221933c4168ca130aa18c681d1cb1e"

    canonical_v2 = registry.resolve("omega_writing_production", 2)
    for unit in canonical_v2.model.units:
        v2_range = unit.character_count
        assert v2_range is not None
        assert (v2_range.target.min, v2_range.target.max) == (9000, 10000)
        assert (v2_range.accepted.min, v2_range.accepted.max) == (7500, 11500)

    canonical_v3 = registry.resolve("omega_writing_production", 3)
    assert canonical_v3.sha256 == (
        "a48cd37881e93deef21cc0802bb29db6495d7c92b440067d4505db9abf7a3e1b"
    )
    canonical_v4 = registry.resolve("omega_writing_production", 4)
    assert canonical_v4.sha256 == (
        "2c3ebe9870a910f62e65397d075274ccfe0bd6af7bc8a5b6613419052612c9be"
    )
    assert canonical_v4.model.unit("INTRO").academic_context is not None
    chapter_context = canonical_v4.model.unit("CH01_A").academic_context
    assert chapter_context is not None
    assert chapter_context.sections[0].heading_level == 1

    alternate = registry.resolve("synthetic_demo", 1)
    assert [unit.unit_id for unit in alternate.model.topological_units()] == [
        "START",
        "BODY",
        "END",
    ]
    assert alternate.model.unit("START").min_characters == 40
    assert not alternate.model.unit("BODY").required_headings

    target_ranges = registry.resolve("synthetic_target_ranges", 1)
    target_range = target_ranges.model.unit("FLEX").character_count
    assert target_range is not None
    assert (target_range.target.min, target_range.target.max) == (40, 60)
    assert (target_range.accepted.min, target_range.accepted.max) == (30, 80)


def test_contract_registry_rejects_hash_drift(tmp_path: Path, repository_root: Path) -> None:
    source = repository_root / "writing_contracts" / "synthetic_demo_v1.yaml"
    contract = tmp_path / "contract.yaml"
    contract.write_bytes(source.read_bytes() + b"\n")
    registry = tmp_path / "registry.yaml"
    registry.write_text(
        "contracts:\n"
        "  - id: synthetic_demo\n"
        "    version: 1\n"
        "    status: active\n"
        "    path: contract.yaml\n"
        '    sha256: "' + "0" * 64 + '"\n',
        encoding="utf-8",
    )
    with pytest.raises(IntegrityError) as captured:
        WritingContractRegistry(registry).resolve("synthetic_demo", 1)
    assert captured.value.code == "WRITING_CONTRACT_HASH_MISMATCH"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda value: value["units"][0].update(max_characters=1),
        lambda value: value["units"][0].update(required_headings=["X"], forbidden_headings=["X"]),
        lambda value: value["units"][0].update(final_heading="X"),
        lambda value: value["units"][1].update(unit_id="START"),
        lambda value: value["units"][1].update(order=10),
        lambda value: value["units"][1].update(continuity_from=["MISSING"]),
        lambda value: value["units"][1].update(continuity_from=["BODY"]),
        lambda value: value["units"][0].update(continuity_from=["BODY"]),
        lambda value: value["units"][0].update(required_headings=["X", "X"]),
    ],
)
def test_contract_rejects_invalid_limits_headings_and_dag(
    repository_root: Path, mutation: Callable[[dict[str, Any]], None]
) -> None:
    value = yaml.safe_load(
        (repository_root / "writing_contracts" / "synthetic_demo_v1.yaml").read_bytes()
    )
    mutation(value)
    with pytest.raises(ValidationError):
        WritingContract.model_validate(value)


@pytest.mark.parametrize(
    "character_count",
    [
        {"target": {"min": 50, "max": 60}, "accepted": {"min": 51, "max": 80}},
        {"target": {"min": 60, "max": 50}, "accepted": {"min": 40, "max": 80}},
        {"target": {"min": 50, "max": 90}, "accepted": {"min": 40, "max": 80}},
    ],
)
def test_contract_rejects_incoherent_target_and_accepted_ranges(
    repository_root: Path, character_count: dict[str, dict[str, int]]
) -> None:
    value = yaml.safe_load(
        (repository_root / "writing_contracts" / "synthetic_target_ranges_v1.yaml").read_bytes()
    )
    value["units"][0]["character_count"] = character_count
    with pytest.raises(ValidationError):
        WritingContract.model_validate(value)


def test_contract_unknown_unit_and_defensive_topology_guard(repository_root: Path) -> None:
    registry = WritingContractRegistry(repository_root / "writing_contracts" / "registry.yaml")
    contract = registry.resolve("synthetic_demo", 1).model
    with pytest.raises(NotFoundError):
        contract.unit("UNKNOWN")
    contract.units[0].continuity_from = ["BODY"]
    with pytest.raises(IntegrityError):
        contract.topological_units()


def _write_registry(tmp_path: Path, content: bytes, *, identity: str = "demo") -> Path:
    contract = tmp_path / "contract.yaml"
    contract.write_bytes(content)
    registry = tmp_path / "registry.yaml"
    registry.write_text(
        "contracts:\n"
        f"  - id: {identity}\n"
        "    version: 1\n"
        "    status: active\n"
        "    path: contract.yaml\n"
        f'    sha256: "{sha256_bytes(content)}"\n',
        encoding="utf-8",
    )
    return registry


def test_contract_registry_reports_missing_invalid_and_identity_errors(
    tmp_path: Path, repository_root: Path
) -> None:
    with pytest.raises(ConfigurationError):
        WritingContractRegistry(tmp_path / "missing.yaml").resolve("x", 1)
    invalid_registry = tmp_path / "invalid.yaml"
    invalid_registry.write_text("contracts: [", encoding="utf-8")
    with pytest.raises(ConfigurationError):
        WritingContractRegistry(invalid_registry).resolve("x", 1)

    registry = WritingContractRegistry(repository_root / "writing_contracts" / "registry.yaml")
    with pytest.raises(NotFoundError):
        registry.resolve("missing", 1)

    missing_file_registry = tmp_path / "missing-file.yaml"
    missing_file_registry.write_text(
        "contracts:\n"
        "  - id: demo\n"
        "    version: 1\n"
        "    status: active\n"
        "    path: absent.yaml\n"
        f'    sha256: "{"0" * 64}"\n',
        encoding="utf-8",
    )
    with pytest.raises(IntegrityError) as missing:
        WritingContractRegistry(missing_file_registry).resolve("demo", 1)
    assert missing.value.code == "WRITING_CONTRACT_FILE_MISSING"

    invalid_contract = b"id: demo\nversion: 1\nseparator: 2\nunits: []\n"
    with pytest.raises(IntegrityError) as invalid:
        WritingContractRegistry(_write_registry(tmp_path, invalid_contract)).resolve("demo", 1)
    assert invalid.value.code == "WRITING_CONTRACT_INVALID"

    valid = (repository_root / "writing_contracts" / "synthetic_demo_v1.yaml").read_bytes()
    with pytest.raises(IntegrityError) as identity:
        WritingContractRegistry(_write_registry(tmp_path, valid, identity="different")).resolve(
            "different", 1
        )
    assert identity.value.code == "WRITING_CONTRACT_IDENTITY_MISMATCH"

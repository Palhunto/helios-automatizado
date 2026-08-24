from pathlib import Path

import pytest

from ebook_pipeline.core.errors import ConfigurationError, IntegrityError, NotFoundError
from ebook_pipeline.prompts import PromptRegistry


def test_academic_prompt_registry_resolves_verified_snapshot(repository_root: Path) -> None:
    resolved = PromptRegistry(repository_root / "prompts" / "registry.yaml").resolve(
        "omega_academic_planning", 1
    )
    assert resolved.sha256 == "15568476f949a1d09769c6ef9906aaf1c0c6cd3a85c258efe84c3bc9e50aeb95"
    assert resolved.content.startswith(b"Qual \xc3\xa9 a disciplina")


def test_prompt_registry_rejects_unknown_and_hash_drift(
    repository_root: Path, tmp_path: Path
) -> None:
    registry = PromptRegistry(repository_root / "prompts" / "registry.yaml")
    with pytest.raises(NotFoundError):
        registry.resolve("missing", 1)

    prompt_dir = tmp_path / "omega_brain"
    prompt_dir.mkdir()
    (prompt_dir / "prompt.txt").write_text("changed", encoding="utf-8")
    (tmp_path / "registry.yaml").write_text(
        "prompts:\n"
        "  - id: test\n"
        "    version: 1\n"
        "    status: active\n"
        "    path: omega_brain/prompt.txt\n"
        f'    sha256: "{"0" * 64}"\n'
        "    domain: test\n",
        encoding="utf-8",
    )
    with pytest.raises(IntegrityError) as captured:
        PromptRegistry(tmp_path / "registry.yaml").resolve("test", 1)
    assert captured.value.code == "PROMPT_HASH_MISMATCH"


def test_prompt_registry_rejects_missing_invalid_duplicate_and_missing_snapshot(
    tmp_path: Path,
) -> None:
    with pytest.raises(ConfigurationError) as missing:
        PromptRegistry(tmp_path / "missing.yaml").resolve("test", 1)
    assert missing.value.code == "PROMPT_REGISTRY_READ_ERROR"

    invalid = tmp_path / "invalid.yaml"
    invalid.write_text("prompts: [", encoding="utf-8")
    with pytest.raises(ConfigurationError) as malformed:
        PromptRegistry(invalid).resolve("test", 1)
    assert malformed.value.code == "PROMPT_REGISTRY_INVALID"

    duplicate = tmp_path / "duplicate.yaml"
    entry = (
        "  - id: test\n    version: 1\n    status: active\n"
        "    path: missing.txt\n"
        f'    sha256: "{"0" * 64}"\n    domain: test\n'
    )
    duplicate.write_text("prompts:\n" + entry + entry, encoding="utf-8")
    with pytest.raises(ConfigurationError) as duplicated:
        PromptRegistry(duplicate).resolve("test", 1)
    assert duplicated.value.code == "PROMPT_REGISTRY_INVALID"

    duplicate.write_text("prompts:\n" + entry, encoding="utf-8")
    with pytest.raises(IntegrityError) as absent:
        PromptRegistry(duplicate).resolve("test", 1)
    assert absent.value.code == "PROMPT_FILE_MISSING"

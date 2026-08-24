import os
from pathlib import Path

import pytest

from ebook_pipeline.core.errors import ArtifactError, ConflictError
from ebook_pipeline.core.hashing import sha256_bytes, sha256_file
from ebook_pipeline.storage.artifacts import ArtifactStore


def test_atomic_write_is_idempotent_and_no_clobber(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "projects")
    store.initialize_project("project-id")
    first = store.write_bytes("project-id", "text.txt", b"valid")
    second = store.write_bytes("project-id", "text.txt", b"valid")
    assert first.sha256 == sha256_bytes(b"valid")
    assert second.already_existed
    with pytest.raises(ConflictError):
        store.write_bytes("project-id", "text.txt", b"different")
    assert store.resolve("project-id", "text.txt").read_bytes() == b"valid"


def test_validator_failure_leaves_no_final_or_temp_file(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "projects")
    store.initialize_project("project-id")

    def reject(_: Path) -> None:
        raise ValueError("invalid artifact")

    with pytest.raises(ArtifactError, match="invalid artifact"):
        store.write_bytes("project-id", "rejected.txt", b"bad", validator=reject)
    root = store.project_root("project-id")
    assert not (root / "rejected.txt").exists()
    assert not list(root.glob(".rejected.txt.*.tmp"))


@pytest.mark.parametrize("same_content", [True, False], ids=["same", "different"])
def test_race_between_precheck_and_promotion_never_overwrites(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, same_content: bool
) -> None:
    store = ArtifactStore(tmp_path / "projects")
    store.initialize_project("project-id")
    expected = b"expected"
    raced = expected if same_content else b"other-process"

    def race_link(source: str | bytes | os.PathLike[str] | os.PathLike[bytes], destination) -> None:  # type: ignore[no-untyped-def]
        Path(destination).write_bytes(raced)
        raise FileExistsError("simulated race")

    monkeypatch.setattr(os, "link", race_link)
    if same_content:
        stored = store.write_bytes("project-id", "raced.txt", expected)
        assert stored.already_existed
    else:
        with pytest.raises(ConflictError):
            store.write_bytes("project-id", "raced.txt", expected)
    assert store.resolve("project-id", "raced.txt").read_bytes() == raced
    assert not list(store.project_root("project-id").glob(".raced.txt.*.tmp"))


def test_atomic_promotion_failure_leaves_no_final_or_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ArtifactStore(tmp_path / "projects")
    store.initialize_project("project-id")

    def fail_link(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise PermissionError("promotion denied")

    monkeypatch.setattr(os, "link", fail_link)
    with pytest.raises(ArtifactError) as captured:
        store.write_bytes("project-id", "promotion.txt", b"complete")
    assert captured.value.code == "ATOMIC_PROMOTION_FAILED"
    root = store.project_root("project-id")
    assert not (root / "promotion.txt").exists()
    assert not list(root.glob(".promotion.txt.*.tmp"))


def test_generic_filesystem_write_failure_is_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ArtifactStore(tmp_path / "projects")
    store.initialize_project("project-id")
    original_open = Path.open

    def fail_temporary_open(path: Path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if path.name.endswith(".tmp"):
            raise OSError("write denied")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", fail_temporary_open)
    with pytest.raises(ArtifactError) as captured:
        store.write_bytes("project-id", "write.txt", b"complete")
    assert captured.value.code == "ARTIFACT_WRITE_FAILED"
    assert not store.resolve("project-id", "write.txt").exists()


def test_temporary_hash_mismatch_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ebook_pipeline.storage import artifacts

    store = ArtifactStore(tmp_path / "projects")
    store.initialize_project("project-id")
    original_hash = sha256_file

    def mismatched_hash(path: Path) -> str:
        if path.name.endswith(".tmp"):
            return "0" * 64
        return original_hash(path)

    monkeypatch.setattr(artifacts, "sha256_file", mismatched_hash)
    with pytest.raises(ArtifactError) as captured:
        store.write_bytes("project-id", "hash.txt", b"complete")
    assert captured.value.code == "ARTIFACT_TEMP_HASH_MISMATCH"
    root = store.project_root("project-id")
    assert not (root / "hash.txt").exists()
    assert not list(root.glob(".hash.txt.*.tmp"))


def test_inspect_missing_and_existing_directory_are_not_artifacts(tmp_path: Path) -> None:
    store = ArtifactStore(tmp_path / "projects")
    root = store.initialize_project("project-id")
    with pytest.raises(ArtifactError) as missing:
        store.inspect("project-id", "missing.txt")
    assert missing.value.code == "ARTIFACT_MISSING"

    (root / "directory.txt").mkdir()
    with pytest.raises(ArtifactError) as directory:
        store.inspect("project-id", "directory.txt")
    assert directory.value.code == "ARTIFACT_MISSING"
    with pytest.raises(ConflictError) as conflict:
        store.write_bytes("project-id", "directory.txt", b"content")
    assert conflict.value.code == "ARTIFACT_DESTINATION_NOT_FILE"

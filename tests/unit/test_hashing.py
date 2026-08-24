from ebook_pipeline.core.hashing import canonical_hash, canonical_json_bytes, sha256_bytes


def test_sha256_uses_exact_bytes() -> None:
    expected = "48582bd628b7c80064780ba9ecce2d435db042b40bd4335a7cea4b4c254e8178"
    assert sha256_bytes(b"helios") == expected
    assert sha256_bytes(b"helios\n") != sha256_bytes(b"helios")


def test_canonical_json_is_independent_of_mapping_order() -> None:
    first = {"b": 2, "a": {"z": True}}
    second = {"a": {"z": True}, "b": 2}
    assert canonical_json_bytes(first) == canonical_json_bytes(second)
    assert canonical_hash(first) == canonical_hash(second)

import json
from pathlib import Path

from jsonschema import Draft202012Validator


def test_visual_schemas_are_valid_draft_2020_12(repository_root: Path) -> None:
    paths = sorted((repository_root / "schemas").glob("visual_*.schema.json"))
    assert paths
    for path in paths:
        Draft202012Validator.check_schema(json.loads(path.read_bytes()))

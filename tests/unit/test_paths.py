from pathlib import Path

import pytest

from ebook_pipeline.core.errors import ConfigurationError
from ebook_pipeline.validation.paths import resolve_under, validate_relative_path


@pytest.mark.parametrize(
    "value",
    ["../escape", "/absolute", "C:/drive", r"folder\file", "NUL.txt", "folder/../file"],
)
def test_unsafe_paths_are_rejected(value: str) -> None:
    with pytest.raises(ConfigurationError):
        validate_relative_path(value)


def test_safe_path_resolves_under_root(tmp_path: Path) -> None:
    result = resolve_under(tmp_path, "text/accepted/section.txt")
    assert result.is_relative_to(tmp_path.resolve())
    assert result.name == "section.txt"

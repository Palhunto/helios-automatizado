from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ebook_pipeline.core.errors import IntegrityError
from ebook_pipeline.pagination import renderer


def test_fingerprint_checks_binary_drift_without_launching_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "chromium.exe"
    executable.write_bytes(b"first binary")
    context = MagicMock()
    driver = context.__enter__.return_value
    driver.chromium.executable_path = str(executable)
    driver.chromium.launch.side_effect = AssertionError("fingerprint must not launch a browser")
    monkeypatch.setattr(renderer, "sync_playwright", lambda: context)
    instance = renderer.ChromiumPaginationRenderer()
    first = instance.fingerprint()
    assert instance.fingerprint() == first
    executable.write_bytes(b"changed binary")
    assert instance.fingerprint().fingerprint != first.fingerprint
    driver.chromium.launch.assert_not_called()
    executable.unlink()
    with pytest.raises(IntegrityError) as error:
        instance.fingerprint()
    assert error.value.code == "PAGINATION_RENDERER_UNAVAILABLE"

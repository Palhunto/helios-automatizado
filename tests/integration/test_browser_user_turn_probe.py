from __future__ import annotations

from pathlib import Path

import pytest
from conftest import ServiceBundle

from ebook_pipeline.browser.user_turn_probe import UserTurnProbeService
from ebook_pipeline.core.errors import IntegrityError


class RecordingUserTurnProbeAdapter:
    def __init__(self) -> None:
        self.conversation_path: str | None = None

    def user_turn_structural_probe(self, conversation_path: str) -> dict[str, object]:
        self.conversation_path = conversation_path
        return {
            "conversation_path": conversation_path,
            "user_turn_count": 1,
            "elements": [
                {
                    "tag_name": "article",
                    "role": None,
                    "aria-label": None,
                    "data-testid": None,
                    "type": None,
                    "text_length": 43,
                    "relationship": "self",
                    "button_count": 1,
                    "group_count": 1,
                }
            ],
        }


def test_user_turn_probe_is_canonical_and_does_not_mutate_sqlite(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
) -> None:
    project = services.projects.create(project_config_path)
    before_database = services.database.path.read_bytes()
    wal_path = services.database.path.with_name(f"{services.database.path.name}-wal")
    before_wal = wal_path.read_bytes() if wal_path.exists() else None
    adapter = RecordingUserTurnProbeAdapter()
    conversation_path = "/c/6a8b792c-af10-83e9-bf1d-7fc7a3e9b99e"
    conversation_url = f"https://chatgpt.com{conversation_path}"

    result = UserTurnProbeService(
        services.config,
        services.database,
        adapter,
    ).run(project.id, conversation_url)

    assert result["database_mode"] == "read_only"
    assert result["project_id"] == project.id
    assert result["conversation_path"] == conversation_path
    assert result["user_turn_count"] == 1
    assert adapter.conversation_path == conversation_path
    assert services.database.path.read_bytes() == before_database
    after_wal = wal_path.read_bytes() if wal_path.exists() else None
    assert after_wal == before_wal or (before_wal is None and after_wal == b"")


@pytest.mark.parametrize(
    "conversation_url",
    [
        "https://chatgpt.com/c/WEB:synthetic",
        "https://chatgpt.com/c/6a8b792c-af10-83e9-bf1d-7fc7a3e9b99e?query=1",
        "https://example.com/c/6a8b792c-af10-83e9-bf1d-7fc7a3e9b99e",
    ],
)
def test_user_turn_probe_rejects_noncanonical_urls_before_browser_access(
    services: ServiceBundle,
    project_config_path: Path,
    conversation_url: str,
) -> None:
    project = services.projects.create(project_config_path)
    adapter = RecordingUserTurnProbeAdapter()

    with pytest.raises(IntegrityError, match="canonical"):
        UserTurnProbeService(
            services.config,
            services.database,
            adapter,
        ).run(project.id, conversation_url)

    assert adapter.conversation_path is None

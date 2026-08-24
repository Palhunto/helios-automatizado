from __future__ import annotations

from pathlib import Path

from conftest import ServiceBundle

from ebook_pipeline.browser.assistant_response_probe import AssistantResponseProbeService


class RecordingAssistantResponseProbeAdapter:
    def __init__(self) -> None:
        self.call: tuple[str, int, str] | None = None

    def assistant_response_text_probe(
        self,
        conversation_path: str,
        assistant_turn_ordinal: int,
        expected_prefix: str,
    ) -> dict[str, object]:
        self.call = (conversation_path, assistant_turn_ordinal, expected_prefix)
        return {
            "conversation_path": conversation_path,
            "assistant_turn_ordinal": assistant_turn_ordinal,
            "assistant_turn_candidate_count": 2,
            "stable_reads": 2,
            "rendered_text_length": 8192,
            "rendered_text_sha256": "a" * 64,
            "starts_with_expected_prefix": True,
        }


def test_assistant_response_probe_is_read_only_and_returns_no_response_content(
    services: ServiceBundle,
    project_config_path: Path,
) -> None:
    project = services.projects.create(project_config_path)
    before_database = services.database.path.read_bytes()
    wal_path = services.database.path.with_name(f"{services.database.path.name}-wal")
    before_wal = wal_path.read_bytes() if wal_path.exists() else None
    adapter = RecordingAssistantResponseProbeAdapter()
    conversation_path = "/c/6a8b8c32-d5e8-83e9-ad85-9d0212b20c32"
    conversation_url = f"https://chatgpt.com{conversation_path}"

    result = AssistantResponseProbeService(
        services.config,
        services.database,
        adapter,
    ).run(
        project.id,
        conversation_url,
        1,
        "Em ambientes empresariais...",
    )

    assert result["database_mode"] == "read_only"
    assert result["starts_with_expected_prefix"] is True
    assert "rendered_text" not in result
    assert adapter.call == (
        conversation_path,
        1,
        "Em ambientes empresariais...",
    )
    assert services.database.path.read_bytes() == before_database
    after_wal = wal_path.read_bytes() if wal_path.exists() else None
    assert after_wal == before_wal or (before_wal is None and after_wal == b"")

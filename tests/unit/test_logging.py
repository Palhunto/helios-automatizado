import json
import logging
from pathlib import Path

from ebook_pipeline.logging_setup import configure_logging, log_event


def test_json_log_has_context_and_redacts_secret(tmp_path: Path) -> None:
    log_path = tmp_path / "helios.jsonl"
    logger = configure_logging("INFO", project_log=log_path)
    log_event(
        logger,
        logging.INFO,
        "request token=super-secret-value",
        project_id="project-1",
        stage_id="create_project",
        unit_id="create_project",
        operation="test",
        status="done",
    )
    payload = json.loads(log_path.read_text(encoding="utf-8"))
    assert payload["project_id"] == "project-1"
    assert payload["operation"] == "test"
    assert "super-secret-value" not in payload["message"]
    assert "[REDACTED]" in payload["message"]

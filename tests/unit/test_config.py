from pathlib import Path

import pytest

from ebook_pipeline.cli import build_parser
from ebook_pipeline.config import load_app_config, load_pipeline_config, load_project_config
from ebook_pipeline.core.errors import ConfigurationError


def test_repository_configs_are_valid(repository_root: Path) -> None:
    project = load_project_config(repository_root / "config" / "project.example.yaml")
    pipeline = load_pipeline_config(repository_root / "config" / "pipeline.yaml")
    assert project.model.project.slug == "exemplo-de-ebook"
    assert pipeline.stages[0].id == "create_project"


def test_pipeline_rejects_cycle(tmp_path: Path) -> None:
    path = tmp_path / "pipeline.yaml"
    path.write_text(
        "stages:\n  - id: first\n    depends_on: [second]\n"
        "  - id: second\n    depends_on: [first]\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="cycle"):
        load_pipeline_config(path)


def test_project_rejects_unknown_field(project_config_path: Path, tmp_path: Path) -> None:
    path = tmp_path / "project.yaml"
    path.write_bytes(project_config_path.read_bytes() + b"unknown: true\n")
    with pytest.raises(ConfigurationError, match="extra_forbidden"):
        load_project_config(path)


def test_cli_override_wins_over_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HELIOS_LOG_LEVEL", "ERROR")
    config = load_app_config(
        {
            "data_dir": tmp_path / "data",
            "projects_dir": tmp_path / "projects",
            "pipeline_config": tmp_path / "pipeline.yaml",
            "log_level": "DEBUG",
        }
    )
    assert config.log_level == "DEBUG"


def test_browser_channel_selects_a_dedicated_default_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.delenv("HELIOS_BROWSER_PROFILE_DIR", raising=False)
    monkeypatch.delenv("HELIOS_BROWSER_CHANNEL", raising=False)
    common: dict[str, object] = {
        "data_dir": tmp_path / "data",
        "projects_dir": tmp_path / "projects",
        "pipeline_config": tmp_path / "pipeline.yaml",
    }

    chrome = load_app_config(common)
    assert chrome.browser_channel == "chrome"
    assert chrome.browser_profile_dir == (
        tmp_path / "local" / "HeliosEbookAutomation" / "chrome-profile"
    ).resolve()

    chromium = load_app_config({**common, "browser_channel": "chromium"})
    assert chromium.browser_channel == "chromium"
    assert chromium.browser_profile_dir == (
        tmp_path / "local" / "HeliosEbookAutomation" / "chatgpt-profile"
    ).resolve()


def test_browser_channel_cli_override_wins_over_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HELIOS_BROWSER_CHANNEL", "chromium")
    config = load_app_config(
        {
            "data_dir": tmp_path / "data",
            "projects_dir": tmp_path / "projects",
            "pipeline_config": tmp_path / "pipeline.yaml",
            "browser_channel": "chrome",
            "browser_profile_dir": tmp_path / "authenticated-chrome-profile",
        }
    )
    assert config.browser_channel == "chrome"
    assert config.browser_profile_dir == (tmp_path / "authenticated-chrome-profile").resolve()


def test_invalid_browser_channel_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="browser_channel"):
        load_app_config(
            {
                "data_dir": tmp_path / "data",
                "projects_dir": tmp_path / "projects",
                "pipeline_config": tmp_path / "pipeline.yaml",
                "browser_channel": "stealth",
            }
        )


def test_cli_exposes_browser_channel_before_browser_command(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        [
            "--browser-channel",
            "chrome",
            "--browser-profile-dir",
            str(tmp_path / "profile"),
            "--no-browser-headless",
            "browser",
            "setup",
        ]
    )
    assert args.browser_channel == "chrome"
    assert args.browser_profile_dir == tmp_path / "profile"
    assert args.browser_headless is False


def test_cli_exposes_explicit_browser_interaction_abandonment() -> None:
    args = build_parser().parse_args(
        [
            "browser",
            "interaction",
            "abandon",
            "project-id",
            "interaction-id",
            "--operator",
            "operator-name",
            "--reason",
            "external effect remained unprovable",
        ]
    )
    assert args.browser_command == "interaction"
    assert args.browser_interaction_command == "abandon"
    assert args.operator == "operator-name"
    assert args.reason == "external effect remained unprovable"


def test_cli_exposes_explicit_browser_reconciliation() -> None:
    args = build_parser().parse_args(
        ["browser", "reconcile", "project-id", "interaction-id"]
    )

    assert args.browser_command == "reconcile"
    assert args.project_id == "project-id"
    assert args.interaction_id == "interaction-id"


def test_cli_exposes_explicit_captured_import_resume() -> None:
    args = build_parser().parse_args(
        [
            "browser",
            "interaction",
            "resume-import",
            "project-id",
            "interaction-id",
        ]
    )

    assert args.browser_command == "interaction"
    assert args.browser_interaction_command == "resume-import"
    assert args.project_id == "project-id"
    assert args.interaction_id == "interaction-id"


def test_cli_exposes_explicit_observational_response_recapture() -> None:
    args = build_parser().parse_args(
        [
            "browser",
            "interaction",
            "recapture-response",
            "project-id",
            "interaction-id",
        ]
    )

    assert args.browser_command == "interaction"
    assert args.browser_interaction_command == "recapture-response"
    assert args.project_id == "project-id"
    assert args.interaction_id == "interaction-id"


def test_invalid_environment_attempt_count_is_typed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HELIOS_MAX_ATTEMPTS", "many")
    with pytest.raises(ConfigurationError, match="must be an integer"):
        load_app_config()

import json
import sqlite3
from pathlib import Path

import pytest

from ebook_pipeline.cli import build_parser, main


def _base_args(tmp_path: Path, repository_root: Path) -> list[str]:
    return [
        "--data-dir",
        str(tmp_path / "data"),
        "--projects-dir",
        str(tmp_path / "projects"),
        "--pipeline-config",
        str(repository_root / "config" / "pipeline.yaml"),
        "--prompt-registry",
        str(repository_root / "prompts" / "registry.yaml"),
        "--writing-contract-registry",
        str(repository_root / "writing_contracts" / "registry.yaml"),
    ]


def test_cli_exposes_composer_spike_with_explicit_project_and_request_artifact() -> None:
    args = build_parser().parse_args(
        [
            "--browser-channel",
            "chrome",
            "browser",
            "composer-spike",
            "project-id",
            "request-artifact-id",
            "--artifact-only",
        ]
    )
    assert args.browser_command == "composer-spike"
    assert args.project_id == "project-id"
    assert args.request_artifact_id == "request-artifact-id"
    assert args.artifact_only is True


def test_cli_exposes_targeted_writing_unit_recovery() -> None:
    args = build_parser().parse_args(
        ["writing", "unit", "recover", "project-id", "CH01_A"]
    )

    assert args.writing_command == "unit"
    assert args.writing_action == "recover"
    assert args.project_id == "project-id"
    assert args.unit_id == "CH01_A"


def test_cli_exposes_single_session_writing_runner() -> None:
    args = build_parser().parse_args(
        ["browser", "run-writing", "project-id"]
    )

    assert args.browser_command == "run-writing"
    assert args.project_id == "project-id"


def test_cli_exposes_read_only_composer_text_spike_for_one_interaction() -> None:
    args = build_parser().parse_args(
        [
            "--browser-channel",
            "chrome",
            "browser",
            "composer-text-spike",
            "project-id",
            "interaction-id",
        ]
    )

    assert args.browser_command == "composer-text-spike"
    assert args.project_id == "project-id"
    assert args.interaction_id == "interaction-id"


def test_cli_exposes_read_only_user_turn_spike_with_canonical_conversation_url() -> None:
    conversation_url = "https://chatgpt.com/c/6a8b792c-af10-83e9-bf1d-7fc7a3e9b99e"
    args = build_parser().parse_args(
        [
            "--browser-channel",
            "chrome",
            "browser",
            "user-turn-spike",
            "project-id",
            "--conversation-url",
            conversation_url,
        ]
    )

    assert args.browser_command == "user-turn-spike"
    assert args.project_id == "project-id"
    assert args.conversation_url == conversation_url


def test_cli_exposes_read_only_assistant_response_spike() -> None:
    conversation_url = "https://chatgpt.com/c/6a8b8c32-d5e8-83e9-ad85-9d0212b20c32"
    args = build_parser().parse_args(
        [
            "--browser-channel",
            "chrome",
            "browser",
            "assistant-response-spike",
            "project-id",
            "--conversation-url",
            conversation_url,
            "--assistant-turn-ordinal",
            "1",
            "--expected-prefix",
            "Em ambientes empresariais...",
        ]
    )

    assert args.browser_command == "assistant-response-spike"
    assert args.assistant_turn_ordinal == 1
    assert args.expected_prefix == "Em ambientes empresariais..."


def test_cli_exposes_read_only_unit_response_completion_spike() -> None:
    args = build_parser().parse_args(
        [
            "--browser-channel",
            "chrome",
            "browser",
            "unit-response-spike",
            "project-id",
            "interaction-id",
        ]
    )

    assert args.browser_command == "unit-response-spike"
    assert args.project_id == "project-id"
    assert args.interaction_id == "interaction-id"


def test_cli_exposes_read_only_conversation_structure_spike() -> None:
    args = build_parser().parse_args(
        [
            "--browser-channel",
            "chrome",
            "browser",
            "conversation-structure-spike",
            "project-id",
            "interaction-id",
        ]
    )

    assert args.browser_command == "conversation-structure-spike"
    assert args.project_id == "project-id"
    assert args.interaction_id == "interaction-id"


def test_cli_exposes_explicit_orphaned_send_block_resolution() -> None:
    args = build_parser().parse_args(
        [
            "browser",
            "interaction",
            "block-orphaned-send",
            "project-id",
            "interaction-id",
        ]
    )

    assert args.browser_command == "interaction"
    assert args.browser_interaction_command == "block-orphaned-send"
    assert args.project_id == "project-id"
    assert args.interaction_id == "interaction-id"


def test_cli_create_list_show_and_validate(
    tmp_path: Path,
    repository_root: Path,
    project_config_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    base = _base_args(tmp_path, repository_root)
    assert main([*base, "project", "create", str(project_config_path)]) == 0
    project = json.loads(capsys.readouterr().out)
    assert main([*base, "project", "list"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed[0]["id"] == project["id"]
    assert main([*base, "project", "show", project["id"]]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["stage_runs"][0]["status"] == "done"
    assert main([*base, "project", "validate", project["id"]]) == 0
    validated = json.loads(capsys.readouterr().out)
    assert validated["valid"] is True


def test_cli_returns_not_found_code(
    tmp_path: Path,
    repository_root: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    base = _base_args(tmp_path, repository_root)
    assert main([*base, "project", "show", "missing"]) == 4
    assert "PROJECT_NOT_FOUND" in capsys.readouterr().err


def test_cli_returns_130_for_operator_interrupt_without_internal_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def interrupted(_args: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("ebook_pipeline.cli.run", interrupted)
    assert main(["project", "list"]) == 130
    stderr = capsys.readouterr().err
    assert "INTERRUPTED" in stderr
    assert "INTERNAL_ERROR" not in stderr


def test_cli_versions_browser_runtime_without_mutating_project_config(
    tmp_path: Path,
    repository_root: Path,
    project_config_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    base = _base_args(tmp_path, repository_root)
    assert main([*base, "project", "create", str(project_config_path)]) == 0
    project = json.loads(capsys.readouterr().out)
    project_id = project["id"]
    historical = (
        tmp_path / "projects" / project["artifact_root"] / project["config_path"]
    )
    original = historical.read_bytes()

    assert main([*base, "project", "runtime", "show", project_id]) == 0
    initial = json.loads(capsys.readouterr().out)
    assert initial["version"] == 0
    assert initial["runtime"]["browser_automation_enabled"] is False

    assert (
        main(
            [
                *base,
                "project",
                "runtime",
                "set",
                project_id,
                "--browser-automation-enabled",
            ]
        )
        == 0
    )
    enabled = json.loads(capsys.readouterr().out)
    assert enabled["version"] == 1
    assert enabled["runtime"]["browser_automation_enabled"] is True
    assert enabled["snapshot_artifact_id"] is not None
    assert historical.read_bytes() == original

    assert main([*base, "project", "validate", project_id]) == 0
    assert json.loads(capsys.readouterr().out)["valid"] is True


def test_cli_recover_reconciles_orphaned_run(
    tmp_path: Path,
    repository_root: Path,
    project_config_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    base = _base_args(tmp_path, repository_root)
    assert main([*base, "project", "create", str(project_config_path)]) == 0
    project = json.loads(capsys.readouterr().out)
    connection = sqlite3.connect(tmp_path / "data" / "helios.db")
    connection.execute(
        "UPDATE stage_runs SET status = 'running', finished_at = NULL WHERE project_id = ?",
        (project["id"],),
    )
    connection.execute("DELETE FROM artifacts WHERE project_id = ?", (project["id"],))
    connection.commit()
    connection.close()

    assert main([*base, "project", "recover", project["id"]]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["recovered"][0]["status"] == "done"


def test_cli_full_academic_flow_survives_service_restart(
    tmp_path: Path,
    repository_root: Path,
    project_config_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from ebook_pipeline.academic.parsers import ANSWER_HEADER, ANSWER_TITLES
    from ebook_pipeline.academic.validators import canonical_questions
    from ebook_pipeline.prompts import PromptRegistry

    base = _base_args(tmp_path, repository_root)
    assert main([*base, "project", "create", str(project_config_path)]) == 0
    project = json.loads(capsys.readouterr().out)
    project_id = project["id"]

    prompt = PromptRegistry(repository_root / "prompts" / "registry.yaml").resolve(
        "omega_academic_planning", 1
    )
    questionnaire = tmp_path / "questionnaire.txt"
    questionnaire.write_text(
        "\n".join(
            f"{number}. {text}" for number, text in enumerate(canonical_questions(prompt), start=1)
        ),
        encoding="utf-8",
    )
    answers = tmp_path / "answers.txt"
    answer_lines = [ANSWER_HEADER]
    for number, title in enumerate(ANSWER_TITLES, start=1):
        answer_lines.extend((f"{number}. {title}", f"[CONFIRMADO] Resposta {number}."))
    answers.write_text("\n".join(answer_lines), encoding="utf-8")
    plan = tmp_path / "plan.md"
    plan.write_text("# Planejamento acadêmico\n", encoding="utf-8")

    assert (
        main(
            [
                *base,
                "academic",
                "questionnaire",
                "import",
                project_id,
                str(questionnaire),
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert main([*base, "academic", "answers", "import", project_id, str(answers)]) == 0
    capsys.readouterr()
    assert main([*base, "academic", "plan", "import", project_id, str(plan)]) == 3
    assert "ACADEMIC_PLAN_NOT_AUTHORIZED" in capsys.readouterr().err
    assert main([*base, "academic", "plan", "authorize", project_id]) == 0
    capsys.readouterr()
    assert main([*base, "academic", "plan", "import", project_id, str(plan)]) == 0
    capsys.readouterr()
    assert main([*base, "academic", "status", project_id]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["academic_plan"]["current"] is True
    assert status["consolidated_answers"]["question_count"] == 5
    assert main([*base, "academic", "validate", project_id]) == 0
    assert json.loads(capsys.readouterr().out)["valid"] is True


def test_cli_complete_manual_writing_flow_with_reduced_contract(
    tmp_path: Path,
    repository_root: Path,
    project_config_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from ebook_pipeline.academic.parsers import ANSWER_HEADER, ANSWER_TITLES
    from ebook_pipeline.academic.validators import canonical_questions
    from ebook_pipeline.prompts import PromptRegistry

    base = _base_args(tmp_path, repository_root)
    assert main([*base, "project", "create", str(project_config_path)]) == 0
    project_id = json.loads(capsys.readouterr().out)["id"]
    prompt = PromptRegistry(repository_root / "prompts" / "registry.yaml").resolve(
        "omega_academic_planning", 1
    )
    questionnaire = tmp_path / "questionnaire.txt"
    questionnaire.write_text(
        "\n".join(
            f"{number}. {text}" for number, text in enumerate(canonical_questions(prompt), start=1)
        ),
        encoding="utf-8",
    )
    answers = tmp_path / "answers.txt"
    lines = [ANSWER_HEADER]
    for number, title in enumerate(ANSWER_TITLES, start=1):
        lines.extend((f"{number}. {title}", f"[CONFIRMADO] Resposta {number}."))
    answers.write_text("\n".join(lines), encoding="utf-8")
    plan = tmp_path / "plan.md"
    plan.write_text("# Plano aceito", encoding="utf-8")
    for arguments in (
        ["academic", "questionnaire", "import", project_id, str(questionnaire)],
        ["academic", "answers", "import", project_id, str(answers)],
        ["academic", "plan", "authorize", project_id],
        ["academic", "plan", "import", project_id, str(plan)],
    ):
        assert main([*base, *arguments]) == 0
        capsys.readouterr()

    assert (
        main(
            [
                *base,
                "writing",
                "context",
                "create",
                project_id,
                "--contract-id",
                "synthetic_demo",
            ]
        )
        == 0
    )
    context = json.loads(capsys.readouterr().out)
    assert "Primeiro, gere um breve resumo" in context["content"]
    acknowledgement = tmp_path / "ack.txt"
    acknowledgement.write_text("Contexto avaliado e confirmado.", encoding="utf-8")
    assert (
        main(
            [
                *base,
                "writing",
                "context",
                "acknowledge",
                project_id,
                str(acknowledgement),
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert main([*base, "writing", "context", "confirm", project_id]) == 0
    capsys.readouterr()

    contents = {
        "START": "Fundamentos iniciais desenvolvidos em extensão válida.",
        "BODY": "Desenvolvimento central consistente, detalhado e conectado ao trecho anterior.",
        "END": "Síntese final preservada e válida.",
    }
    for unit_id, content in contents.items():
        assert main([*base, "writing", "unit", "prepare", project_id, unit_id]) == 0
        prepared = json.loads(capsys.readouterr().out)
        assert "Continue na mesma conversa" in prepared["content"]
        output = tmp_path / f"{unit_id}.txt"
        output.write_text(content, encoding="utf-8")
        assert (
            main(
                [
                    *base,
                    "writing",
                    "unit",
                    "import",
                    project_id,
                    unit_id,
                    str(output),
                ]
            )
            == 0
        )
        assert json.loads(capsys.readouterr().out)["disposition"] == "accepted"

    assert main([*base, "writing", "status", project_id]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["production_set"]["current"] is True
    assert main([*base, "writing", "consolidate", project_id]) == 0
    capsys.readouterr()
    assert main([*base, "writing", "consolidated", "show", project_id]) == 0
    assert "Fundamentos iniciais" in json.loads(capsys.readouterr().out)["content"]
    assert main([*base, "writing", "validate", project_id]) == 0
    assert json.loads(capsys.readouterr().out)["valid"] is True

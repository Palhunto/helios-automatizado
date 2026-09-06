from __future__ import annotations

import argparse
import base64
import json
import logging
import sys
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import asdict
from pathlib import Path

from ebook_pipeline.academic.models import AcademicDocument, DocumentKind
from ebook_pipeline.academic.service import AcademicService
from ebook_pipeline.browser.assistant_response_probe import AssistantResponseProbeService
from ebook_pipeline.browser.chatgpt import ChatGPTWebAdapter
from ebook_pipeline.browser.composer_probe import ComposerProbeService
from ebook_pipeline.browser.composer_text_probe import ComposerTextProbeService
from ebook_pipeline.browser.conversation_structure_probe import (
    ConversationStructureProbeService,
)
from ebook_pipeline.browser.recovery import BrowserRecovery
from ebook_pipeline.browser.service import BrowserAutomationService
from ebook_pipeline.browser.unit_response_probe import UnitResponseProbeService
from ebook_pipeline.browser.user_turn_probe import UserTurnProbeService
from ebook_pipeline.config import AppConfig, load_app_config, load_pipeline_config
from ebook_pipeline.core.errors import ConfigurationError, HeliosError
from ebook_pipeline.core.models import Project, RecoveryResult, StageRun, ValidationIssue
from ebook_pipeline.core.projects import ProjectService
from ebook_pipeline.core.recovery import RecoveryService
from ebook_pipeline.core.runtime_config import ResolvedProjectRuntime
from ebook_pipeline.logging_setup import configure_logging, log_event
from ebook_pipeline.pagination.service import PaginationService
from ebook_pipeline.storage.artifacts import ArtifactStore
from ebook_pipeline.storage.database import Database
from ebook_pipeline.visual_planning.enrichment import VisualAnchorService
from ebook_pipeline.visual_planning.finalization import VisualFinalizationService
from ebook_pipeline.visual_planning.service import VisualPlanningService
from ebook_pipeline.writing.models import TextUnitSubmission, WritingUnitRecoveryResult
from ebook_pipeline.writing.service import WritingService


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ebook", description="Hélios Ebook Automation")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--projects-dir", type=Path)
    parser.add_argument("--pipeline-config", type=Path)
    parser.add_argument("--prompt-registry", type=Path)
    parser.add_argument("--writing-contract-registry", type=Path)
    parser.add_argument("--pagination-layout-registry", type=Path)
    parser.add_argument("--log-level")
    parser.add_argument("--max-attempts", type=int)
    parser.add_argument("--browser-channel", choices=("chrome", "chromium"))
    parser.add_argument("--browser-profile-dir", type=Path)
    parser.add_argument("--browser-timeout-seconds", type=int)
    parser.add_argument(
        "--browser-headless", action=argparse.BooleanOptionalAction, default=None
    )

    commands = parser.add_subparsers(dest="command", required=True)
    project = commands.add_parser("project", help="Manage local ebook projects")
    project_commands = project.add_subparsers(dest="project_command", required=True)

    create = project_commands.add_parser("create", help="Create a project from YAML config")
    create.add_argument("config", type=Path)

    project_commands.add_parser("list", help="List projects")

    show = project_commands.add_parser("show", help="Show a project and its stage runs")
    show.add_argument("project_id")

    validate = project_commands.add_parser("validate", help="Validate DB/filesystem consistency")
    validate.add_argument("project_id")

    recover = project_commands.add_parser("recover", help="Reconcile orphaned running stage runs")
    recover.add_argument("project_id")

    runtime = project_commands.add_parser(
        "runtime", help="Show or version mutable project runtime settings"
    )
    runtime_commands = runtime.add_subparsers(dest="project_runtime_action", required=True)
    runtime_show = runtime_commands.add_parser("show", help="Show effective runtime settings")
    runtime_show.add_argument("project_id")
    runtime_set = runtime_commands.add_parser("set", help="Create an immutable runtime version")
    runtime_set.add_argument("project_id")
    runtime_set.add_argument(
        "--browser-automation-enabled",
        action=argparse.BooleanOptionalAction,
        required=True,
    )

    academic = commands.add_parser("academic", help="Manage academic planning artifacts")
    academic_commands = academic.add_subparsers(dest="academic_command", required=True)

    academic_status = academic_commands.add_parser("status", help="Show academic status")
    academic_status.add_argument("project_id")
    academic_validate = academic_commands.add_parser("validate", help="Validate academic state")
    academic_validate.add_argument("project_id")

    questionnaire = academic_commands.add_parser("questionnaire")
    questionnaire_commands = questionnaire.add_subparsers(dest="academic_action", required=True)
    questionnaire_import = questionnaire_commands.add_parser("import")
    questionnaire_import.add_argument("project_id")
    questionnaire_import.add_argument("file")
    questionnaire_show = questionnaire_commands.add_parser("show")
    questionnaire_show.add_argument("project_id")
    questionnaire_show.add_argument("--version", type=int)
    questionnaire_show.add_argument("--raw", action="store_true")
    questionnaire_confirm = questionnaire_commands.add_parser("confirm")
    questionnaire_confirm.add_argument("project_id")
    questionnaire_confirm.add_argument("--raw-version", type=int, required=True)

    answers = academic_commands.add_parser("answers")
    answers_commands = answers.add_subparsers(dest="academic_action", required=True)
    answers_import = answers_commands.add_parser("import")
    answers_import.add_argument("project_id")
    answers_import.add_argument("file")
    answers_show = answers_commands.add_parser("show")
    answers_show.add_argument("project_id")
    answers_show.add_argument("--version", type=int)
    answers_show.add_argument("--raw", action="store_true")

    plan = academic_commands.add_parser("plan")
    plan_commands = plan.add_subparsers(dest="academic_action", required=True)
    plan_authorize = plan_commands.add_parser("authorize")
    plan_authorize.add_argument("project_id")
    plan_import = plan_commands.add_parser("import")
    plan_import.add_argument("project_id")
    plan_import.add_argument("file")
    plan_show = plan_commands.add_parser("show")
    plan_show.add_argument("project_id")
    plan_show.add_argument("--version", type=int)
    plan_show.add_argument("--raw", action="store_true")

    writing = commands.add_parser("writing", help="Manage contract-driven text production")
    writing_commands = writing.add_subparsers(dest="writing_command", required=True)
    writing_status = writing_commands.add_parser("status", help="Show writing status")
    writing_status.add_argument("project_id")
    writing_validate = writing_commands.add_parser("validate", help="Validate writing state")
    writing_validate.add_argument("project_id")
    writing_consolidate = writing_commands.add_parser("consolidate", help="Consolidate text")
    writing_consolidate.add_argument("project_id")

    context = writing_commands.add_parser("context")
    context_commands = context.add_subparsers(dest="writing_action", required=True)
    context_create = context_commands.add_parser("create")
    context_create.add_argument("project_id")
    context_create.add_argument("--contract-id", default="omega_writing_production")
    context_create.add_argument("--contract-version", type=int, default=1)
    context_create.add_argument("--request-prompt-id", default="helios_writing_unit_request")
    context_create.add_argument("--request-prompt-version", type=int, default=1)
    context_show = context_commands.add_parser("show")
    context_show.add_argument("project_id")
    context_show.add_argument("--version", type=int)
    context_show.add_argument("--content-only", action="store_true")
    context_ack = context_commands.add_parser("acknowledge")
    context_ack.add_argument("project_id")
    context_ack.add_argument("file")
    context_confirm = context_commands.add_parser("confirm")
    context_confirm.add_argument("project_id")
    context_confirm.add_argument("--raw-version", type=int)

    unit = writing_commands.add_parser("unit")
    unit_commands = unit.add_subparsers(dest="writing_action", required=True)
    unit_prepare = unit_commands.add_parser("prepare")
    unit_prepare.add_argument("project_id")
    unit_prepare.add_argument("unit_id")
    unit_prepare.add_argument("--reprocess", action="store_true")
    unit_prepare.add_argument("--content-only", action="store_true")
    unit_recover = unit_commands.add_parser("recover")
    unit_recover.add_argument("project_id")
    unit_recover.add_argument("unit_id")
    unit_import = unit_commands.add_parser("import")
    unit_import.add_argument("project_id")
    unit_import.add_argument("unit_id")
    unit_import.add_argument("file")
    unit_show = unit_commands.add_parser("show")
    unit_show.add_argument("project_id")
    unit_show.add_argument("unit_id")
    unit_show.add_argument("--version", type=int)
    unit_show.add_argument("--raw", action="store_true")
    unit_confirm = unit_commands.add_parser("confirm")
    unit_confirm.add_argument("project_id")
    unit_confirm.add_argument("unit_id")
    unit_confirm.add_argument("--raw-version", type=int, required=True)

    consolidated = writing_commands.add_parser("consolidated")
    consolidated_commands = consolidated.add_subparsers(dest="writing_action", required=True)
    consolidated_show = consolidated_commands.add_parser("show")
    consolidated_show.add_argument("project_id")
    consolidated_show.add_argument("--version", type=int)

    pagination = commands.add_parser("pagination", help="Create canonical local pagination")
    pagination_commands = pagination.add_subparsers(dest="pagination_command", required=True)
    pagination_create = pagination_commands.add_parser("create", help="Create or resume snapshot")
    pagination_create.add_argument("project_id")
    pagination_create.add_argument("--layout-id", default="helios_pagination_layout")
    pagination_create.add_argument("--layout-version", type=int, default=1)
    pagination_show = pagination_commands.add_parser("show", help="Show snapshot manifest")
    pagination_show.add_argument("project_id")
    pagination_show.add_argument("--version", type=int)
    pagination_status = pagination_commands.add_parser("status", help="Show current/stale state")
    pagination_status.add_argument("project_id")
    pagination_validate = pagination_commands.add_parser("validate", help="Validate snapshots")
    pagination_validate.add_argument("project_id")

    visual = commands.add_parser("visual", help="Manage visual planning imports")
    visual_commands = visual.add_subparsers(dest="visual_command", required=True)
    visual_plan = visual_commands.add_parser("plan", help="Manage visual plans")
    visual_plan_commands = visual_plan.add_subparsers(dest="visual_action", required=True)
    visual_plan_import = visual_plan_commands.add_parser("import", help="Import V2 raw")
    visual_plan_import.add_argument("project_id")
    visual_plan_import.add_argument("file")
    visual_plan_show = visual_plan_commands.add_parser("show", help="Show an import")
    visual_plan_show.add_argument("project_id")
    visual_plan_show.add_argument("--version", type=int)
    visual_plan_show.add_argument("--raw", action="store_true")
    visual_plan_status = visual_plan_commands.add_parser("status", help="Show current state")
    visual_plan_status.add_argument("project_id")
    visual_plan_validate = visual_plan_commands.add_parser("validate", help="Validate imports")
    visual_plan_validate.add_argument("project_id")
    visual_finalize = visual_plan_commands.add_parser(
        "finalize", help="Accept or resume visual plan"
    )
    visual_finalize.add_argument("project_id")
    visual_finalize.add_argument("--plan-version", type=int)
    visual_manifest = visual_plan_commands.add_parser(
        "manifest", help="Read accepted visual manifest"
    )
    visual_manifest.add_argument("project_id")
    visual_manifest.add_argument("--version", type=int)
    visual_anchor = visual_commands.add_parser("anchor", help="Enrich and repair literal anchors")
    anchor_commands = visual_anchor.add_subparsers(dest="visual_action", required=True)
    for anchor_action in ("request", "import", "show", "recover"):
        anchor_parser = anchor_commands.add_parser(anchor_action)
        anchor_parser.add_argument("project_id")
        anchor_parser.add_argument("figure_id")
        if anchor_action == "import":
            anchor_parser.add_argument("file")
        if anchor_action == "show":
            anchor_parser.add_argument("--version", type=int)
    visual_figure = visual_commands.add_parser("figure", help="Inspect visual figures")
    visual_figure_commands = visual_figure.add_subparsers(
        dest="visual_action", required=True
    )
    visual_figure_list = visual_figure_commands.add_parser("list", help="List figures")
    visual_figure_list.add_argument("project_id")
    visual_figure_list.add_argument("--plan-version", type=int)
    visual_figure_show = visual_figure_commands.add_parser("show", help="Show a figure")
    visual_figure_show.add_argument("project_id")
    visual_figure_show.add_argument("figure_id")

    browser = commands.add_parser("browser", help="Automate ChatGPT Plus through Playwright")
    browser_commands = browser.add_subparsers(dest="browser_command", required=True)
    browser_setup = browser_commands.add_parser(
        "setup", help="Open/check the dedicated persistent profile"
    )
    browser_setup.add_argument("--wait-seconds", type=int, default=0)
    browser_spike = browser_commands.add_parser(
        "capture-spike", help="Compare rendered and Copy capture"
    )
    browser_spike.add_argument(
        "--sample-kind", choices=("acknowledgement", "long_unit"), required=True
    )
    browser_spike.add_argument("--error-hold-seconds", type=int, default=30)
    browser_composer_spike = browser_commands.add_parser(
        "composer-spike",
        help="Probe headed Chrome composer insertion without sending",
    )
    browser_composer_spike.add_argument("project_id")
    browser_composer_spike.add_argument("request_artifact_id")
    browser_composer_spike.add_argument(
        "--artifact-only",
        action="store_true",
        help="Probe only the exact request artifact, without synthetic payloads",
    )
    browser_composer_text_spike = browser_commands.add_parser(
        "composer-text-spike",
        help="Compare read-only text representations of the currently filled composer",
    )
    browser_composer_text_spike.add_argument("project_id")
    browser_composer_text_spike.add_argument("interaction_id")
    browser_user_turn_spike = browser_commands.add_parser(
        "user-turn-spike",
        help="Inspect one existing user-turn subtree without browser or database mutation",
    )
    browser_user_turn_spike.add_argument("project_id")
    browser_user_turn_spike.add_argument("--conversation-url", required=True)
    browser_assistant_response_spike = browser_commands.add_parser(
        "assistant-response-spike",
        help="Verify rendered capture scope for one existing assistant response",
    )
    browser_assistant_response_spike.add_argument("project_id")
    browser_assistant_response_spike.add_argument("--conversation-url", required=True)
    browser_assistant_response_spike.add_argument(
        "--assistant-turn-ordinal", type=int, required=True
    )
    browser_assistant_response_spike.add_argument("--expected-prefix", required=True)
    browser_unit_response_spike = browser_commands.add_parser(
        "unit-response-spike",
        help="Audit completion signals for one persisted ordinal unit response",
    )
    browser_unit_response_spike.add_argument("project_id")
    browser_unit_response_spike.add_argument("interaction_id")
    browser_conversation_structure_spike = browser_commands.add_parser(
        "conversation-structure-spike",
        help="Audit turn, branch, and lazy-rendering structure without sending",
    )
    browser_conversation_structure_spike.add_argument("project_id")
    browser_conversation_structure_spike.add_argument("interaction_id")
    browser_status = browser_commands.add_parser("status", help="Show persisted browser state")
    browser_status.add_argument("project_id")
    browser_validate = browser_commands.add_parser("validate", help="Validate browser artifacts")
    browser_validate.add_argument("project_id")
    browser_context = browser_commands.add_parser("context-run", help="Load one WritingContext")
    browser_context.add_argument("project_id")
    browser_context.add_argument("--context-id")
    browser_unit = browser_commands.add_parser("unit-run", help="Produce one writing unit")
    browser_unit.add_argument("project_id")
    browser_unit.add_argument("unit_id")
    browser_unit.add_argument("--context-id")
    browser_retry = browser_commands.add_parser("retry", help="Retry a rejected unit explicitly")
    browser_retry.add_argument("project_id")
    browser_retry.add_argument("interaction_id")
    browser_recover = browser_commands.add_parser("recover", help="Recover interrupted sends")
    browser_recover.add_argument("project_id")
    browser_reconcile = browser_commands.add_parser(
        "reconcile", help="Reconcile one externally proven blocked interaction"
    )
    browser_reconcile.add_argument("project_id")
    browser_reconcile.add_argument("interaction_id")
    browser_continue = browser_commands.add_parser(
        "continue", help="Advance one eligible browser operation"
    )
    browser_continue.add_argument("project_id")
    browser_run_writing = browser_commands.add_parser(
        "run-writing", help="Run eligible writing units in one browser session"
    )
    browser_run_writing.add_argument("project_id")
    browser_interaction = browser_commands.add_parser(
        "interaction", help="Inspect or resolve browser interactions"
    )
    browser_interaction_commands = browser_interaction.add_subparsers(
        dest="browser_interaction_command", required=True
    )
    browser_interaction_show = browser_interaction_commands.add_parser(
        "show", help="Show a persisted browser interaction and its audit trail"
    )
    browser_interaction_show.add_argument("project_id")
    browser_interaction_show.add_argument("interaction_id", nargs="?")
    browser_interaction_abandon = browser_interaction_commands.add_parser(
        "abandon", help="Abandon an unprovable blocked external attempt"
    )
    browser_interaction_abandon.add_argument("project_id")
    browser_interaction_abandon.add_argument("interaction_id")
    browser_interaction_abandon.add_argument("--operator", required=True)
    browser_interaction_abandon.add_argument("--reason", required=True)
    browser_interaction_block_orphaned = browser_interaction_commands.add_parser(
        "block-orphaned-send",
        help="Promote one proven orphaned sending interaction to blocked",
    )
    browser_interaction_block_orphaned.add_argument("project_id")
    browser_interaction_block_orphaned.add_argument("interaction_id")
    browser_interaction_resume_import = browser_interaction_commands.add_parser(
        "resume-import",
        help="Import one already-captured response without browser I/O",
    )
    browser_interaction_resume_import.add_argument("project_id")
    browser_interaction_resume_import.add_argument("interaction_id")
    browser_interaction_recapture = browser_interaction_commands.add_parser(
        "recapture-response",
        help="Recapture one persisted unit response without sending",
    )
    browser_interaction_recapture.add_argument("project_id")
    browser_interaction_recapture.add_argument("interaction_id")
    return parser


def _app_config(args: argparse.Namespace) -> AppConfig:
    return load_app_config(
        {
            "data_dir": args.data_dir,
            "projects_dir": args.projects_dir,
            "pipeline_config": args.pipeline_config,
            "prompt_registry": args.prompt_registry,
            "writing_contract_registry": args.writing_contract_registry,
            "pagination_layout_registry": args.pagination_layout_registry,
            "log_level": args.log_level,
            "max_attempts": args.max_attempts,
            "browser_channel": args.browser_channel,
            "browser_profile_dir": args.browser_profile_dir,
            "browser_timeout_seconds": args.browser_timeout_seconds,
            "browser_headless": args.browser_headless,
        }
    )


def _services(
    config: AppConfig,
) -> tuple[
    ProjectService,
    RecoveryService,
    ArtifactStore,
    AcademicService,
    WritingService,
    PaginationService,
    VisualPlanningService,
]:
    load_pipeline_config(config.pipeline_config)
    database = Database(config.database_path)
    store = ArtifactStore(config.projects_dir)
    academic = AcademicService(config, database, store)
    writing = WritingService(config, database, store, academic)
    pagination = PaginationService(config, database, store, writing)
    return (
        ProjectService(config, database, store),
        RecoveryService(config, database, store),
        store,
        academic,
        writing,
        pagination,
        VisualPlanningService(config, database, store, pagination),
    )


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)


def _project_payload(project: Project) -> dict[str, object]:
    return asdict(project)


def _runtime_payload(runtime: ResolvedProjectRuntime) -> dict[str, object]:
    return {
        "project_id": runtime.project_id,
        "runtime": runtime.model.model_dump(mode="json"),
        "snapshot_artifact_id": runtime.snapshot_artifact_id,
        "snapshot_sha256": runtime.snapshot_sha256,
        "source": runtime.source,
        "source_config_sha256": runtime.source_config_sha256,
        "version": runtime.version,
    }


def _run_payload(run: StageRun) -> dict[str, object]:
    payload = asdict(run)
    payload["status"] = run.status.value
    return payload


def _issue_payload(issue: ValidationIssue) -> dict[str, object]:
    return asdict(issue)


def _recovery_payload(result: RecoveryResult) -> dict[str, object]:
    payload = asdict(result)
    payload["previous_status"] = result.previous_status.value
    payload["status"] = result.status.value
    return payload


def _academic_document_payload(document: AcademicDocument) -> dict[str, object]:
    payload = asdict(document)
    payload["document_kind"] = document.document_kind.value
    payload["acceptance_status"] = document.acceptance_status.value
    return payload


def _input_bytes(value: str) -> bytes:
    if value == "-":
        return sys.stdin.buffer.read()
    try:
        return Path(value).read_bytes()
    except OSError as exc:
        from ebook_pipeline.core.errors import ConfigurationError

        raise ConfigurationError(
            "INPUT_READ_ERROR", f"Could not read input {value!r}: {exc}"
        ) from exc


def _configure_project_log(
    config: AppConfig, store: ArtifactStore, project: Project
) -> logging.Logger:
    path = store.resolve(project.artifact_root, "logs/helios.jsonl")
    return configure_logging(config.log_level, project_log=path)


def run(args: argparse.Namespace) -> int:
    config = _app_config(args)
    logger = configure_logging(config.log_level)
    projects, recovery, store, academic, writing, pagination, visual = _services(config)

    if args.command == "academic":
        return _run_academic(args, config, projects, store, academic)
    if args.command == "writing":
        return _run_writing(args, config, projects, store, writing)
    if args.command == "pagination":
        return _run_pagination(args, config, projects, store, pagination)
    if args.command == "visual":
        return _run_visual(args, config, projects, store, visual)
    if args.command == "browser":
        return _run_browser(args, config, projects, store, writing)

    command = args.project_command

    if command == "create":
        created = projects.create(args.config.resolve())
        logger = _configure_project_log(config, store, created)
        log_event(
            logger,
            logging.INFO,
            "Project is ready",
            project_id=created.id,
            stage_id="create_project",
            unit_id="create_project",
            operation="project.create",
            status="done",
        )
        print(_json(_project_payload(created)))
        return 0

    if command == "list":
        print(_json([_project_payload(project) for project in projects.list_projects()]))
        return 0

    if command == "runtime":
        project = projects.get(args.project_id)
        logger = _configure_project_log(config, store, project)
        if args.project_runtime_action == "show":
            runtime = projects.runtime_config.resolve(project.id)
            operation = "project.runtime.show"
        elif args.project_runtime_action == "set":
            runtime = projects.runtime_config.set_browser_automation(
                project.id, args.browser_automation_enabled
            )
            operation = "project.runtime.set"
        else:
            raise AssertionError(
                f"Unhandled project runtime action: {args.project_runtime_action}"
            )
        print(_json(_runtime_payload(runtime)))
        log_event(
            logger,
            logging.INFO,
            "Project runtime configuration resolved",
            project_id=project.id,
            stage_id="project_runtime_config",
            unit_id="runtime:update",
            operation=operation,
            status="done",
        )
        return 0

    project = projects.get(args.project_id)
    logger = _configure_project_log(config, store, project)
    if command == "show":
        print(
            _json(
                {
                    "project": _project_payload(project),
                    "stage_runs": [_run_payload(item) for item in projects.runs(project.id)],
                }
            )
        )
        log_event(
            logger,
            logging.INFO,
            "Project inspected",
            project_id=project.id,
            operation="project.show",
            status="done",
        )
        return 0
    if command == "validate":
        issues = projects.validate(project.id)
        print(
            _json(
                {
                    "project_id": project.id,
                    "valid": not issues,
                    "issues": [_issue_payload(issue) for issue in issues],
                }
            )
        )
        log_event(
            logger,
            logging.INFO if not issues else logging.ERROR,
            "Project validation completed",
            project_id=project.id,
            operation="project.validate",
            status="done" if not issues else "failed",
            error_code=None if not issues else "PROJECT_INTEGRITY_FAILED",
        )
        return 0 if not issues else 5
    if command == "recover":
        results = recovery.recover(project.id)
        print(
            _json(
                {
                    "project_id": project.id,
                    "recovered": [_recovery_payload(result) for result in results],
                }
            )
        )
        unresolved = any(result.status.value != "done" for result in results)
        log_event(
            logger,
            logging.WARNING if unresolved else logging.INFO,
            "Project recovery completed",
            project_id=project.id,
            operation="project.recover",
            status="pending_retry" if unresolved else "done",
        )
        return 5 if unresolved else 0
    raise AssertionError(f"Unhandled project command: {command}")


def _run_academic(
    args: argparse.Namespace,
    config: AppConfig,
    projects: ProjectService,
    store: ArtifactStore,
    academic: AcademicService,
) -> int:
    project = projects.get(args.project_id)
    logger = _configure_project_log(config, store, project)
    command = args.academic_command
    if command == "status":
        print(_json(academic.status(project.id)))
        return 0
    if command == "validate":
        issues = academic.validate(project.id)
        print(
            _json(
                {
                    "project_id": project.id,
                    "valid": not issues,
                    "issues": [_issue_payload(issue) for issue in issues],
                }
            )
        )
        return 0 if not issues else 5

    action = args.academic_action
    if command == "questionnaire":
        if action == "import":
            document = academic.import_questionnaire(project.id, _input_bytes(args.file))
        elif action == "confirm":
            document = academic.confirm_questionnaire(project.id, args.raw_version)
        else:
            return _show_academic(
                academic, project.id, DocumentKind.QUESTIONNAIRE, args.version, args.raw
            )
    elif command == "answers":
        if action == "import":
            document = academic.import_answers(project.id, _input_bytes(args.file))
        else:
            return _show_academic(
                academic,
                project.id,
                DocumentKind.CONSOLIDATED_ANSWERS,
                args.version,
                args.raw,
            )
    elif command == "plan":
        if action == "authorize":
            authorization = academic.authorize_plan(project.id)
            print(_json(asdict(authorization)))
            return 0
        if action == "import":
            document = academic.import_plan(project.id, _input_bytes(args.file))
        else:
            return _show_academic(
                academic, project.id, DocumentKind.ACADEMIC_PLAN, args.version, args.raw
            )
    else:
        raise AssertionError(f"Unhandled academic command: {command}")

    log_event(
        logger,
        logging.INFO,
        "Academic operation completed",
        project_id=project.id,
        stage_id=document.document_kind.value,
        unit_id=document.document_kind.value,
        operation=f"academic.{command}.{action}",
        status=document.acceptance_status.value,
    )
    print(_json(_academic_document_payload(document)))
    return 0


def _show_academic(
    academic: AcademicService,
    project_id: str,
    kind: DocumentKind,
    version: int | None,
    raw: bool,
) -> int:
    document, content = academic.document(project_id, kind, version=version, raw=raw)
    print(
        _json(
            {
                "document": _academic_document_payload(document),
                "content": content.decode("utf-8"),
            }
        )
    )
    return 0


def _writing_submission_payload(item: TextUnitSubmission) -> dict[str, object]:
    payload = asdict(item)
    payload["disposition"] = item.disposition.value
    return payload


def _writing_unit_recovery_payload(
    item: WritingUnitRecoveryResult,
) -> dict[str, object]:
    return {
        "project_id": item.project_id,
        "preparation": {
            "context_id": item.context_id,
            "id": item.preparation_id,
            "previous_version": item.previous_version,
            "unit_id": item.unit_id,
            "version": item.version,
        },
        "stage_run": {
            "finished_at": item.finished_at,
            "id": item.stage_run_id,
            "previous_status": item.previous_stage_run_status.value,
            "status": item.stage_run_status.value,
        },
        "artifacts": [asdict(artifact) for artifact in item.artifacts],
        "recovered": item.recovered,
    }


def _run_writing(
    args: argparse.Namespace,
    config: AppConfig,
    projects: ProjectService,
    store: ArtifactStore,
    writing: WritingService,
) -> int:
    project = projects.get(args.project_id)
    _configure_project_log(config, store, project)
    command = args.writing_command
    if command == "status":
        print(_json(writing.status(project.id)))
        return 0
    if command == "validate":
        issues = writing.validate(project.id)
        print(
            _json(
                {
                    "issues": [_issue_payload(issue) for issue in issues],
                    "project_id": project.id,
                    "valid": not issues,
                }
            )
        )
        return 0 if not issues else 5
    if command == "consolidate":
        print(_json(asdict(writing.consolidate(project.id))))
        return 0

    action = args.writing_action
    if command == "context":
        if action == "create":
            writing_context = writing.create_context(
                project.id,
                contract_id=args.contract_id,
                contract_version=args.contract_version,
                request_prompt_id=args.request_prompt_id,
                request_prompt_version=args.request_prompt_version,
            )
            _, content = writing.context_package(project.id, writing_context.version)
            print(
                _json(
                    {
                        "context": asdict(writing_context),
                        "content": content.decode("utf-8"),
                    }
                )
            )
            return 0
        if action == "show":
            writing_context, content = writing.context_package(project.id, args.version)
            if args.content_only:
                print(content.decode("utf-8"))
                return 0
            print(
                _json(
                    {
                        "content": content.decode("utf-8"),
                        "context": writing.contexts.payload(writing_context),
                    }
                )
            )
            return 0
        if action == "acknowledge":
            print(_json(asdict(writing.acknowledge_context(project.id, _input_bytes(args.file)))))
            return 0
        print(_json(asdict(writing.confirm_context(project.id, args.raw_version))))
        return 0

    if command == "unit":
        if action == "recover":
            print(
                _json(
                    _writing_unit_recovery_payload(
                        writing.recover_unit_preparation(project.id, args.unit_id)
                    )
                )
            )
            return 0
        if action == "prepare":
            preparation = writing.prepare_unit(project.id, args.unit_id, reprocess=args.reprocess)
            _, content = writing.unit_request(project.id, args.unit_id)
            if args.content_only:
                print(content.decode("utf-8"))
                return 0
            print(
                _json(
                    {
                        "content": content.decode("utf-8"),
                        "preparation": asdict(preparation),
                    }
                )
            )
            return 0
        if action == "import":
            submission = writing.import_unit(project.id, args.unit_id, _input_bytes(args.file))
            print(_json(_writing_submission_payload(submission)))
            return 0
        if action == "confirm":
            submission = writing.confirm_unit(project.id, args.unit_id, args.raw_version)
            print(_json(_writing_submission_payload(submission)))
            return 0
        submission, content = writing.unit(
            project.id,
            args.unit_id,
            version=args.version,
            raw=args.raw,
        )
        validation_report = writing.unit_validation_report(project.id, submission)
        print(
            _json(
                {
                    "content": content.decode("utf-8"),
                    "submission": _writing_submission_payload(submission),
                    "validation_report": validation_report,
                }
            )
        )
        return 0

    if command == "consolidated" and action == "show":
        consolidation, content = writing.consolidated(project.id, args.version)
        print(
            _json(
                {
                    "consolidation": asdict(consolidation),
                    "content": content.decode("utf-8"),
                }
            )
        )
        return 0
    raise AssertionError(f"Unhandled writing command: {command} {action}")


def _run_pagination(
    args: argparse.Namespace,
    config: AppConfig,
    projects: ProjectService,
    store: ArtifactStore,
    pagination: PaginationService,
) -> int:
    project = projects.get(args.project_id)
    _configure_project_log(config, store, project)
    command = args.pagination_command
    if command == "create":
        snapshot = pagination.create(
            project.id,
            layout_id=args.layout_id,
            layout_version=args.layout_version,
        )
        print(_json(asdict(snapshot)))
        return 0
    if command == "show":
        snapshot, manifest = pagination.show(project.id, args.version)
        print(_json({"manifest": manifest, "snapshot": asdict(snapshot)}))
        return 0
    if command == "status":
        print(_json(pagination.status(project.id)))
        return 0
    if command == "validate":
        issues = pagination.validate(project.id)
        print(
            _json(
                {
                    "issues": [_issue_payload(issue) for issue in issues],
                    "project_id": project.id,
                    "valid": not issues,
                }
            )
        )
        return 0 if not issues else 5
    raise AssertionError(f"Unhandled pagination command: {command}")


def _run_visual(
    args: argparse.Namespace,
    config: AppConfig,
    projects: ProjectService,
    store: ArtifactStore,
    visual: VisualPlanningService,
) -> int:
    project = projects.get(args.project_id)
    _configure_project_log(config, store, project)
    command = args.visual_command
    action = args.visual_action
    anchors = VisualAnchorService(visual)
    finalizations = VisualFinalizationService(visual)
    if command == "anchor":
        if action == "request":
            print(_json(anchors.request(project.id, args.figure_id)))
            return 0
        if action == "import":
            anchor, report = anchors.import_raw(project.id, args.figure_id, _input_bytes(args.file))
        elif action == "recover":
            anchor, report = anchors.recover(project.id, args.figure_id)
        else:
            anchor, report = anchors.show(project.id, args.figure_id, args.version)
        print(_json({"anchor": asdict(anchor), "validation_report": report}))
        return 0 if anchor.disposition == "valid" else 2
    if command == "plan" and action == "finalize":
        accepted, manifest = finalizations.finalize(project.id, args.plan_version)
        print(_json({"finalization": asdict(accepted), "manifest": manifest}))
        return 0
    if command == "plan" and action == "manifest":
        accepted, manifest = finalizations.show(project.id, args.version)
        print(_json({"finalization": asdict(accepted), "manifest": manifest}))
        return 0
    if command == "plan" and action == "import":
        plan, report = visual.import_raw(project.id, _input_bytes(args.file))
        print(_json({"plan": asdict(plan), "validation_report": report}))
        return 0 if plan.disposition == "valid" else 2
    if command == "plan" and action == "show":
        plan, figures, report, raw = visual.show(project.id, args.version)
        payload: dict[str, object] = {
            "plan": asdict(plan),
            "figures": [asdict(figure) for figure in figures],
            "validation_report": report,
        }
        if args.raw:
            try:
                payload["raw"] = raw.decode("utf-8")
            except UnicodeDecodeError:
                payload["raw_base64"] = base64.b64encode(raw).decode("ascii")
                payload["raw_encoding"] = "base64"
        print(_json(payload))
        return 0
    if command == "plan" and action == "status":
        print(_json({
            **visual.status(project.id), "finalization": finalizations.status(project.id)
        }))
        return 0
    if command == "plan" and action == "validate":
        issues = visual.validate(project.id) + finalizations.validate(project.id)
        print(
            _json(
                {
                    "issues": [_issue_payload(issue) for issue in issues],
                    "project_id": project.id,
                    "valid": not issues,
                }
            )
        )
        return 0 if not issues else 5
    if command == "figure" and action == "list":
        plan, figures = visual.list_figures(project.id, args.plan_version)
        print(_json({"plan": asdict(plan), "figures": [asdict(item) for item in figures]}))
        return 0
    if command == "figure" and action == "show":
        print(_json(asdict(visual.show_figure(project.id, args.figure_id))))
        return 0
    raise AssertionError(f"Unhandled visual command: {command} {action}")


def _run_browser(
    args: argparse.Namespace,
    config: AppConfig,
    projects: ProjectService,
    store: ArtifactStore,
    writing: WritingService,
) -> int:
    composer_spike = args.browser_command == "composer-spike"
    composer_text_spike = args.browser_command == "composer-text-spike"
    user_turn_spike = args.browser_command == "user-turn-spike"
    assistant_response_spike = args.browser_command == "assistant-response-spike"
    unit_response_spike = args.browser_command == "unit-response-spike"
    conversation_structure_spike = (
        args.browser_command == "conversation-structure-spike"
    )
    adapter = ChatGPTWebAdapter(
        profile_dir=config.browser_profile_dir,
        browser_channel=config.browser_channel,
        base_url=config.browser_base_url,
        headless=(
            False
            if (
                composer_spike
                or composer_text_spike
                or user_turn_spike
                or assistant_response_spike
                or unit_response_spike
                or conversation_structure_spike
            )
            else config.browser_headless
        ),
        timeout_seconds=config.browser_timeout_seconds,
        capture_method_version=config.browser_capture_method_version,
    )
    service = BrowserAutomationService(
        config, Database(config.database_path), store, adapter, writing
    )
    try:
        if args.browser_command == "setup":
            state = adapter.ensure_ready()
            if args.wait_seconds < 0:
                raise ConfigurationError(
                    "BROWSER_SETUP_WAIT_INVALID", "--wait-seconds must not be negative"
                )
            if (
                args.wait_seconds
                and not config.browser_headless
            ):
                import time

                deadline = time.monotonic() + args.wait_seconds
                while time.monotonic() < deadline:
                    time.sleep(1)
                    state = adapter.ensure_ready()
                    if state.value == "challenge":
                        break
            print(
                _json(
                    {
                        "browser_channel": config.browser_channel,
                        "profile_dir": str(config.browser_profile_dir),
                        "session_state": state.value,
                        "secrets_persisted_by_helios": False,
                    }
                )
            )
            return 0 if state.value == "ready" else 5
        if args.browser_command == "capture-spike":
            if args.error_hold_seconds < 0:
                raise ConfigurationError(
                    "BROWSER_SPIKE_HOLD_INVALID", "--error-hold-seconds must not be negative"
                )
            print(_json(asdict(adapter.capture_spike(args.sample_kind))))
            return 0
        if composer_spike:
            result = ComposerProbeService(
                config,
                Database(config.database_path),
                store,
                adapter,
            ).run(
                args.project_id,
                args.request_artifact_id,
                artifact_only=args.artifact_only,
            )
            print(_json(result))
            return 0
        if composer_text_spike:
            result = ComposerTextProbeService(
                config,
                Database(config.database_path),
                store,
                adapter,
            ).run(args.project_id, args.interaction_id)
            print(_json(result))
            return 0
        if user_turn_spike:
            result = UserTurnProbeService(
                config,
                Database(config.database_path),
                adapter,
            ).run(args.project_id, args.conversation_url)
            print(_json(result))
            return 0
        if assistant_response_spike:
            result = AssistantResponseProbeService(
                config,
                Database(config.database_path),
                adapter,
            ).run(
                args.project_id,
                args.conversation_url,
                args.assistant_turn_ordinal,
                args.expected_prefix,
            )
            print(_json(result))
            return 0
        if unit_response_spike:
            result = UnitResponseProbeService(
                config,
                Database(config.database_path),
                adapter,
            ).run(args.project_id, args.interaction_id)
            print(_json(result))
            return 0
        if conversation_structure_spike:
            result = ConversationStructureProbeService(
                config,
                Database(config.database_path),
                adapter,
            ).run(args.project_id, args.interaction_id)
            print(_json(result))
            return 0
        project = projects.get(args.project_id)
        logger = _configure_project_log(config, store, project)
        if args.browser_command == "status":
            result = service.status(project.id)
            print(_json(result))
            _log_browser_command(logger, project.id, "status", "done")
            return 0
        if args.browser_command == "validate":
            issues = service.validate(project.id)
            print(
                _json(
                    {
                        "issues": [_issue_payload(issue) for issue in issues],
                        "project_id": project.id,
                        "valid": not issues,
                    }
                )
            )
            _log_browser_command(
                logger,
                project.id,
                "validate",
                "done" if not issues else "failed",
                error_code=None if not issues else "BROWSER_INTEGRITY_FAILED",
            )
            return 0 if not issues else 5
        if args.browser_command == "context-run":
            result = service.run_context(project.id, args.context_id)
            print(_json(result))
            _log_browser_command(logger, project.id, "context-run", str(result["status"]))
            return 0
        if args.browser_command == "unit-run":
            result = service.run_unit(
                project.id, args.unit_id, context_id=args.context_id
            )
            print(_json(result))
            _log_browser_command(
                logger,
                project.id,
                "unit-run",
                str(result["status"]),
                unit_id=args.unit_id,
            )
            return 0
        if args.browser_command == "retry":
            result = service.retry_rejected(project.id, args.interaction_id)
            print(_json(result))
            _log_browser_command(logger, project.id, "retry", str(result["status"]))
            return 0
        if args.browser_command == "recover":
            report = BrowserRecovery(service).recover(project.id)
            print(_json(report))
            skipped = report.get("skipped")
            unresolved = isinstance(skipped, list) and any(
                isinstance(item, dict) and item.get("requires_action") is True
                for item in skipped
            )
            _log_browser_command(
                logger,
                project.id,
                "recover",
                "blocked" if unresolved else "done",
            )
            return 5 if unresolved else 0
        if args.browser_command == "reconcile":
            result = service.reconcile_blocked_interaction(
                project.id, args.interaction_id
            )
            print(_json(result))
            _log_browser_command(logger, project.id, "reconcile", str(result["outcome"]))
            return 0
        if args.browser_command == "continue":
            result = service.continue_project(project.id)
            print(_json(result))
            _log_browser_command(logger, project.id, "continue", str(result["status"]))
            return 0
        if args.browser_command == "run-writing":
            result = service.run_writing(project.id)
            print(_json(result))
            blocked = result.get("blocked_interaction") is not None
            _log_browser_command(
                logger,
                project.id,
                "run-writing",
                str(result["stop_reason"]),
                error_code=(
                    str(result.get("error_code", "BROWSER_WRITING_BATCH_BLOCKED"))
                    if blocked
                    else None
                ),
            )
            return 5 if blocked else 0
        if args.browser_command == "interaction":
            if args.browser_interaction_command == "show":
                result = service.interaction_show(project.id, args.interaction_id)
                print(_json(result))
                _log_browser_command(logger, project.id, "interaction.show", "done")
                return 0
            if args.browser_interaction_command == "abandon":
                result = service.abandon_interaction(
                    project.id,
                    args.interaction_id,
                    operator=args.operator,
                    reason=args.reason,
                )
                print(_json(result))
                _log_browser_command(logger, project.id, "interaction.abandon", "done")
                return 0
            if args.browser_interaction_command == "block-orphaned-send":
                result = service.block_orphaned_sending_interaction(
                    project.id, args.interaction_id
                )
                print(_json(result))
                _log_browser_command(
                    logger,
                    project.id,
                    "interaction.block-orphaned-send",
                    str(result["outcome"]),
                )
                return 0
            if args.browser_interaction_command == "resume-import":
                result = service.resume_captured_import(
                    project.id, args.interaction_id
                )
                print(_json(result))
                _log_browser_command(
                    logger,
                    project.id,
                    "interaction.resume-import",
                    str(result["outcome"]),
                )
                return 0
            if args.browser_interaction_command == "recapture-response":
                result = service.recapture_unit_response(
                    project.id, args.interaction_id
                )
                print(_json(result))
                _log_browser_command(
                    logger,
                    project.id,
                    "interaction.recapture-response",
                    str(result["outcome"]),
                )
                return 0
            raise AssertionError(
                f"Unhandled browser interaction command: {args.browser_interaction_command}"
            )
        raise AssertionError(f"Unhandled browser command: {args.browser_command}")
    except Exception:
        if (
            args.browser_command == "capture-spike"
            and not config.browser_headless
            and config.log_level == "DEBUG"
            and args.error_hold_seconds > 0
        ):
            logging.getLogger("ebook_pipeline").debug(
                "browser_capture_spike checkpoint=error_hold seconds=%s",
                args.error_hold_seconds,
            )
            import time

            time.sleep(args.error_hold_seconds)
        raise
    finally:
        adapter.close()


def _log_browser_command(
    logger: logging.Logger,
    project_id: str,
    operation: str,
    status: str,
    *,
    unit_id: str | None = None,
    error_code: str | None = None,
) -> None:
    log_event(
        logger,
        logging.ERROR if error_code else logging.INFO,
        "Browser operation completed",
        project_id=project_id,
        stage_id="chatgpt_browser_automation",
        unit_id=unit_id or "context",
        operation=f"browser.{operation}",
        status=status,
        error_code=error_code,
    )


def _configure_unicode_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            with suppress(OSError, ValueError):
                reconfigure(encoding="utf-8", errors="backslashreplace")


def main(argv: Sequence[str] | None = None) -> int:
    _configure_unicode_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return run(args)
    except HeliosError as exc:
        logger = logging.getLogger("ebook_pipeline")
        log_event(
            logger,
            logging.ERROR,
            exc.message,
            operation="cli",
            status="failed",
            error_code=exc.code,
        )
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        logging.getLogger("ebook_pipeline").warning(
            "Operator interrupted operation; persisted browser state requires normal recovery"
        )
        print(
            "INTERRUPTED: operator interrupted operation; browser recovery may be required",
            file=sys.stderr,
        )
        return 130
    except Exception:
        logging.getLogger("ebook_pipeline").exception("Unexpected internal error")
        print("INTERNAL_ERROR: unexpected internal error", file=sys.stderr)
        return 1

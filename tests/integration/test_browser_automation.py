from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Never

import pytest
from browser_fakes import (
    DEFAULT_CONVERSATION_PATH,
    OLD_CONVERSATION_PATH,
    FakeChatAdapter,
)
from conftest import ServiceBundle
from test_writing_flow import _answers, _questionnaire

import ebook_pipeline.browser.service as browser_service_module
from ebook_pipeline.browser.composer_text_probe import ComposerTextProbeService
from ebook_pipeline.browser.fingerprints import transport_fingerprint
from ebook_pipeline.browser.models import (
    BootstrapCandidate,
    ConversationStatus,
    InteractionKind,
    InteractionStatus,
    SessionState,
    TurnInspection,
    TurnState,
)
from ebook_pipeline.browser.recovery import BrowserRecovery
from ebook_pipeline.browser.repositories import (
    BrowserConversationInvalidationRepository,
    BrowserConversationRepository,
    BrowserInteractionRepository,
    BrowserInteractionResolutionRepository,
)
from ebook_pipeline.browser.service import BrowserAutomationService
from ebook_pipeline.core.errors import ConflictError, IntegrityError
from ebook_pipeline.core.hashing import sha256_bytes
from ebook_pipeline.core.ids import utc_now
from ebook_pipeline.core.models import RunStatus
from ebook_pipeline.storage.repositories import (
    ArtifactRepository,
    ProjectRepository,
    StageRunRepository,
)
from ebook_pipeline.writing.models import TextProductionSet, TextUnitSubmission
from ebook_pipeline.writing.repositories import (
    AcknowledgementRepository,
    SubmissionRepository,
)


def _enabled_config(tmp_path: Path, project_config_path: Path) -> Path:
    content = project_config_path.read_text(encoding="utf-8").replace(
        "browser_automation_enabled: false", "browser_automation_enabled: true"
    )
    suffix = (
        tmp_path.name
        if tmp_path.name in {"other", "third", "wrong", "timeout"}
        else "primary"
    )
    content = content.replace("exemplo-de-ebook", f"exemplo-de-ebook-{suffix}")
    path = tmp_path / "browser-project.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _automation_context(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
) -> tuple[str, str]:
    project = services.projects.create(_enabled_config(tmp_path, project_config_path))
    services.academic.import_questionnaire(project.id, _questionnaire(repository_root))
    services.academic.import_answers(project.id, _answers())
    services.academic.authorize_plan(project.id)
    plan = (
        "# Plano acadêmico\n\n## Planejamento dos capítulos\n\n"
        "### Capítulo 1\nFunção educacional, objetivo e conteúdos do primeiro capítulo.\n\n"
        "### Capítulo 2\nFunção educacional, objetivo e conteúdos do segundo capítulo.\n"
    ).encode()
    services.academic.import_plan(project.id, plan)
    context = services.writing.create_context(
        project.id,
        contract_id="synthetic_browser",
        contract_version=1,
        request_prompt_id="helios_writing_unit_request",
        request_prompt_version=2,
    )
    return project.id, context.id


def _service(
    services: ServiceBundle,
    adapter: FakeChatAdapter,
    fault: str | None = None,
) -> BrowserAutomationService:
    def hook(checkpoint: str) -> None:
        if checkpoint == fault:
            raise RuntimeError(checkpoint)

    return BrowserAutomationService(
        services.config,
        services.database,
        services.store,
        adapter,
        services.writing,
        fault_hook=hook,
    )


def _blocked_unprovable_context(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
) -> tuple[str, str, FakeChatAdapter, BrowserAutomationService]:
    project_id, context_id = _automation_context(
        services, tmp_path, project_config_path, repository_root
    )
    adapter = FakeChatAdapter()
    adapter.inspect_override = TurnState.NOT_SENT
    zero_timeout = services.config.model_copy(update={"browser_timeout_seconds": 0})
    browser = BrowserAutomationService(
        zero_timeout,
        services.database,
        services.store,
        adapter,
        services.writing,
    )
    with pytest.raises(IntegrityError) as captured:
        browser.run_context(project_id, context_id)
    assert captured.value.code == "BROWSER_SEND_NOT_PROVABLE"
    adapter.inspect_override = None
    return project_id, context_id, adapter, browser


def _recovered_items(report: dict[str, object]) -> list[dict[str, object]]:
    value = report["recovered"]
    assert isinstance(value, list) and all(isinstance(item, dict) for item in value)
    return value


def _skipped_items(report: dict[str, object]) -> list[dict[str, object]]:
    value = report["skipped"]
    assert isinstance(value, list) and all(isinstance(item, dict) for item in value)
    return value


def _captured_unit_for_import_resume(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
) -> tuple[str, str, FakeChatAdapter, BrowserAutomationService]:
    project_id, context_id = _automation_context(
        services, tmp_path, project_config_path, repository_root
    )
    adapter = FakeChatAdapter()
    browser = _service(services, adapter)
    browser.run_context(project_id, context_id)
    with services.database.connection() as connection:
        acknowledgement = AcknowledgementRepository(connection).latest(context_id)
    assert acknowledgement is not None
    services.writing.confirm_context_by_id(
        project_id, context_id, acknowledgement.raw_version
    )
    adapter.next_response = (
        "A unidade capturada possui conteúdo suficiente para validação.\n\n"
        "A continuidade permanece íntegra e vinculada à preparação exata."
    )
    with pytest.raises(RuntimeError, match="browser.response_captured"):
        _service(services, adapter, "browser.response_captured").run_unit(
            project_id, "START", context_id=context_id
        )
    with services.database.connection() as connection:
        row = connection.execute(
            "SELECT id FROM browser_interactions WHERE project_id = ? "
            "AND context_id = ? AND kind = 'unit_request' ORDER BY created_at DESC LIMIT 1",
            (project_id, context_id),
        ).fetchone()
    assert row is not None
    return project_id, str(row["id"]), adapter, browser


def _confirmed_browser_context(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
    adapter: FakeChatAdapter,
) -> tuple[str, str, BrowserAutomationService]:
    project_id, context_id = _automation_context(
        services, tmp_path, project_config_path, repository_root
    )
    browser = _service(services, adapter)
    browser.run_context(project_id, context_id)
    with services.database.connection() as connection:
        acknowledgement = AcknowledgementRepository(connection).latest(context_id)
    assert acknowledgement is not None
    services.writing.confirm_context_by_id(
        project_id, context_id, acknowledgement.raw_version
    )
    return project_id, context_id, browser


def _persisted_unit_turn_anchor(pre_send_count: int = 1) -> dict[str, object]:
    return {
        "version": 1,
        "conversation_path": DEFAULT_CONVERSATION_PATH,
        "pre_send_user_turn_count": pre_send_count,
        "pre_send_assistant_turn_count": pre_send_count,
        "tail": [
            {"role": "user", "id": "persisted-anchor-user"},
            {"role": "assistant", "id": "persisted-anchor-assistant"},
        ],
    }


class _LocalProofRecoveryAdapter(FakeChatAdapter):
    def __init__(
        self,
        *,
        persist_selected_user_id: bool = True,
        sent_assistant_id: str | None = None,
        recovery_user_id: str | None = None,
        recovery_assistant_id: str | None = None,
    ) -> None:
        super().__init__()
        self.persist_selected_user_id = persist_selected_user_id
        self.sent_assistant_id = sent_assistant_id
        self.recovery_user_id = recovery_user_id
        self.recovery_assistant_id = recovery_assistant_id
        self.reject_legacy_inspection = False
        self.legacy_inspection_count = 0
        self.partial_local_ambiguity_on_structural_call: int | None = None
        self.structural_inspection_count = 0

    @staticmethod
    def _local_ids(fingerprint: str) -> tuple[str, str]:
        return f"user-{fingerprint}", f"assistant-{fingerprint}"

    def inspect_turn(self, fingerprint: str) -> TurnInspection:
        self.legacy_inspection_count += 1
        if self.reject_legacy_inspection:
            raise AssertionError("legacy fingerprint proof must not run for local sent recovery")
        return super().inspect_turn(fingerprint)

    def inspect_sent_turn_structure(self, fingerprint: str) -> TurnInspection:
        observed = super().inspect_sent_turn_structure(fingerprint)
        self.structural_inspection_count += 1
        user_id, assistant_id = self._local_ids(fingerprint)
        if (
            self.partial_local_ambiguity_on_structural_call
            == self.structural_inspection_count
        ):
            return TurnInspection(
                TurnState.AMBIGUOUS,
                observed.conversation_path,
                evidence={
                    "proof_kind": "post_send_local_successor_v1",
                    "selected_user_id": user_id,
                    "selected_assistant_id": "request-placeholder-unit-response",
                },
                observed_user_turn_fingerprint=(
                    observed.observed_user_turn_fingerprint
                ),
            )
        if observed.state not in {TurnState.STREAMING, TurnState.COMPLETE}:
            return observed
        evidence: dict[str, object] = {
            "proof_kind": "post_send_local_successor_v1",
            "selected_assistant_id": self.sent_assistant_id or assistant_id,
        }
        if self.persist_selected_user_id:
            evidence["selected_user_id"] = user_id
        return TurnInspection(
            observed.state,
            observed.conversation_path,
            observed.response_text,
            evidence=evidence,
            observed_user_turn_fingerprint=observed.observed_user_turn_fingerprint,
        )

    def inspect_reconciliation_unit_turn(
        self,
        turn_binding: int | dict[str, object],
        expected_conversation_path: str | None = None,
    ) -> TurnInspection:
        observed = super().inspect_reconciliation_unit_turn(
            turn_binding,
            expected_conversation_path,
        )
        evidence = dict(observed.evidence or {})
        raw_ordinal = (
            turn_binding.get("pre_send_user_turn_count")
            if isinstance(turn_binding, dict)
            else turn_binding
        )
        if (
            isinstance(raw_ordinal, int)
            and self.current_path is not None
            and raw_ordinal < len(self.conversations.get(self.current_path, {}))
        ):
            fingerprint = list(self.conversations[self.current_path])[raw_ordinal]
            user_id, assistant_id = self._local_ids(fingerprint)
            evidence["selected_user_id"] = self.recovery_user_id or user_id
            evidence["selected_assistant_id"] = (
                self.recovery_assistant_id or assistant_id
            )
        return TurnInspection(
            observed.state,
            observed.conversation_path,
            observed.response_text,
            evidence=evidence,
            observed_user_turn_fingerprint=observed.observed_user_turn_fingerprint,
        )


def _sent_local_unit_for_recovery(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
    *,
    persist_selected_user_id: bool = True,
    sent_assistant_id: str | None = None,
    recovery_user_id: str | None = None,
    recovery_assistant_id: str | None = None,
) -> tuple[str, str, _LocalProofRecoveryAdapter]:
    project_id, context_id = _automation_context(
        services,
        tmp_path,
        project_config_path,
        repository_root,
    )
    adapter = _LocalProofRecoveryAdapter(
        persist_selected_user_id=persist_selected_user_id,
        sent_assistant_id=sent_assistant_id,
        recovery_user_id=recovery_user_id,
        recovery_assistant_id=recovery_assistant_id,
    )
    browser = _service(services, adapter)
    browser.run_context(project_id, context_id)
    with services.database.connection() as connection:
        acknowledgement = AcknowledgementRepository(connection).latest(context_id)
    assert acknowledgement is not None
    services.writing.confirm_context_by_id(
        project_id,
        context_id,
        acknowledgement.raw_version,
    )
    adapter.next_response = (
        "A unidade recuperada desenvolve conteúdo suficiente para validação.\n\n"
        "A análise preserva continuidade, clareza e densidade acadêmica."
    )
    with pytest.raises(RuntimeError, match="browser.response_stored"):
        _service(services, adapter, "browser.response_stored").run_unit(
            project_id,
            "START",
            context_id=context_id,
        )
    with services.database.connection() as connection:
        row = connection.execute(
            "SELECT id FROM browser_interactions WHERE project_id = ? "
            "AND context_id = ? AND kind = 'unit_request' ORDER BY created_at DESC LIMIT 1",
            (project_id, context_id),
        ).fetchone()
    assert row is not None
    return project_id, str(row["id"]), adapter


def _blocked_partial_local_unit_for_recovery(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
    *,
    recovery_assistant_id: str | None = None,
) -> tuple[str, str, str, _LocalProofRecoveryAdapter, BrowserAutomationService]:
    adapter = _LocalProofRecoveryAdapter(
        recovery_assistant_id=recovery_assistant_id,
    )
    project_id, context_id, browser = _confirmed_browser_context(
        services,
        tmp_path,
        project_config_path,
        repository_root,
        adapter,
    )
    adapter.next_response = (
        "A unidade recuperada desenvolve conteúdo suficiente para validação direta."
    )
    adapter.partial_local_ambiguity_on_structural_call = (
        adapter.structural_inspection_count + 1
    )
    with pytest.raises(IntegrityError) as captured:
        browser.run_unit(project_id, "START", context_id=context_id)
    assert captured.value.code == "BROWSER_TURN_AMBIGUOUS"
    with services.database.connection() as connection:
        item = BrowserInteractionRepository(connection).latest_for_context_kind(
            context_id,
            InteractionKind.UNIT_REQUEST,
            "START",
        )
    assert item is not None
    return project_id, context_id, item.id, adapter, browser


def test_composer_text_spike_reads_bound_request_without_database_mutation(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project_id, context_id = _automation_context(
        services, tmp_path, project_config_path, repository_root
    )
    interaction = _service(services, FakeChatAdapter()).prepare_context(
        project_id, context_id
    )
    with (
        services.database.connection() as connection,
        services.database.transaction(connection),
    ):
        connection.execute(
            "UPDATE browser_conversations SET status = 'ready', conversation_path = ?, "
            "first_turn_fingerprint = ?, ready_at = updated_at WHERE id = ?",
            (
                DEFAULT_CONVERSATION_PATH,
                interaction.transport_fingerprint,
                interaction.conversation_id,
            ),
        )
    _context, expected_bytes = services.writing.context_package_by_id(
        project_id, context_id
    )

    class DiagnosticAdapter:
        def __init__(self) -> None:
            self.expected_text: str | None = None

        def composer_text_diagnostic(
            self, expected_text: str, conversation_path: str
        ) -> dict[str, object]:
            assert conversation_path == DEFAULT_CONVERSATION_PATH
            self.expected_text = expected_text
            return {
                "expected_length": len(expected_text),
                "comparison_representation": "inner_text",
                "first_divergence_index": 10,
                "common_prefix_length": 10,
                "common_suffix_length": 20,
                "length_delta": -100,
                "representations": {},
            }

    adapter = DiagnosticAdapter()
    before = services.config.database_path.read_bytes()
    result = ComposerTextProbeService(
        services.config.model_copy(update={"browser_channel": "chrome"}),
        services.database,
        services.store,
        adapter,
    ).run(project_id, interaction.id)
    after = services.config.database_path.read_bytes()

    assert adapter.expected_text == expected_bytes.decode("utf-8")
    assert result["database_mode"] == "read_only"
    assert result["interaction_id"] == interaction.id
    assert result["request_artifact_id"] == interaction.request_artifact_id
    assert result["conversation_path"] == DEFAULT_CONVERSATION_PATH
    assert before == after


def test_context_and_unit_use_clean_content_same_conversation_and_pause_review(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project_id, context_id = _automation_context(
        services, tmp_path, project_config_path, repository_root
    )
    adapter = FakeChatAdapter()
    browser = _service(services, adapter)
    result = browser.run_context(project_id, context_id)
    assert result["status"] == "imported" and result["confirmation_required"] is True
    assert len(adapter.sent_messages) == 1
    repeated = browser.run_context(project_id, context_id)
    assert repeated["id"] == result["id"]
    assert len(adapter.sent_messages) == 1
    assert "manifest_sha256" not in adapter.sent_messages[0][1]
    with services.database.connection() as connection:
        acknowledgement = AcknowledgementRepository(connection).confirmed(context_id)
        assert acknowledgement is None
        raw = AcknowledgementRepository(connection).latest(context_id)
    assert raw is not None
    services.writing.confirm_context_by_id(project_id, context_id, raw.raw_version)

    adapter.next_response = (
        "A abertura desenvolve conteúdo suficiente para validação.\n\n"
        "A análise continua com densidade acadêmica adequada."
    )
    unit_result = browser.run_unit(project_id, "START", context_id=context_id)
    assert unit_result["status"] == "imported"
    assert unit_result["disposition"] == "review_required"
    assert unit_result["paused"] is True
    assert {path for path, _ in adapter.sent_messages} == {DEFAULT_CONVERSATION_PATH}
    assert "<contexto_da_unidade>" in adapter.sent_messages[-1][1]
    assert "Primeiro, gere um breve resumo" not in adapter.sent_messages[-1][1]


def test_writing_runner_processes_two_accepted_units_in_one_adapter_session(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class TrackingAdapter(FakeChatAdapter):
        def __init__(self) -> None:
            super().__init__()
            self.created_conversations = 0

        def create_conversation(self) -> None:
            self.created_conversations += 1
            super().create_conversation()

    adapter = TrackingAdapter()
    project_id, _context_id, browser = _confirmed_browser_context(
        services, tmp_path, project_config_path, repository_root, adapter
    )
    adapter.next_response = (
        "Texto acadêmico com extensão objetiva suficiente para aceitação direta."
    )
    adapter.created_conversations = 0
    production_set_calls = 0
    original_production_set = services.writing.production_set

    def counted_production_set(project: str) -> TextProductionSet:
        nonlocal production_set_calls
        production_set_calls += 1
        return original_production_set(project)

    monkeypatch.setattr(services.writing, "production_set", counted_production_set)

    result = browser.run_writing(project_id)

    assert result == {
        "project_id": project_id,
        "processed_units": ["START", "END"],
        "recovered_interactions": [],
        "accepted_units": ["START", "END"],
        "warning_count": 0,
        "warning_units": [],
        "review_required_unit": None,
        "blocked_interaction": None,
        "stop_reason": "complete",
    }
    assert production_set_calls >= 3
    assert adapter.created_conversations == 0
    assert [path for path, _text in adapter.sent_messages[-2:]] == [
        DEFAULT_CONVERSATION_PATH,
        DEFAULT_CONVERSATION_PATH,
    ]


def test_writing_runner_recovers_blocked_post_send_before_next_unit_without_resend(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    adapter = FakeChatAdapter()
    project_id, context_id, _browser = _confirmed_browser_context(
        services, tmp_path, project_config_path, repository_root, adapter
    )
    adapter.next_response = (
        "Texto acadêmico com extensão objetiva suficiente para aceitação direta."
    )
    adapter.inspect_override = TurnState.NOT_SENT
    zero_timeout = services.config.model_copy(update={"browser_timeout_seconds": 0})
    with pytest.raises(IntegrityError) as captured:
        BrowserAutomationService(
            zero_timeout,
            services.database,
            services.store,
            adapter,
            services.writing,
        ).run_unit(project_id, "START", context_id=context_id)
    assert captured.value.code == "BROWSER_SEND_NOT_PROVABLE"
    sends_before_runner = len(adapter.sent_messages)
    with services.database.connection() as connection:
        blocked = BrowserInteractionRepository(connection).latest_for_context_kind(
            context_id, InteractionKind.UNIT_REQUEST, "START"
        )
    assert blocked is not None
    adapter.inspect_override = None

    result = _service(services, adapter).run_writing(project_id)

    assert result["stop_reason"] == "complete"
    assert result["recovered_interactions"] == [blocked.id]
    assert result["processed_units"] == ["START", "END"]
    assert result["accepted_units"] == ["START", "END"]
    assert len(adapter.sent_messages) == sends_before_runner + 1


def test_writing_runner_reconciles_in_process_post_send_failure_without_resend(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    class ReconcileOnceAdapter(FakeChatAdapter):
        def __init__(self) -> None:
            super().__init__()
            self.require_reconcile = False

        def inspect_sent_turn_structure(self, fingerprint: str) -> TurnInspection:
            if self.require_reconcile:
                self.require_reconcile = False
                raise ConflictError(
                    "BROWSER_SEND_REQUIRES_RECONCILE",
                    "Synthetic post-Send uncertainty",
                )
            return super().inspect_sent_turn_structure(fingerprint)

    adapter = ReconcileOnceAdapter()
    project_id, _context_id, browser = _confirmed_browser_context(
        services, tmp_path, project_config_path, repository_root, adapter
    )
    adapter.require_reconcile = True
    adapter.next_response = (
        "Texto acadêmico com extensão objetiva suficiente para aceitação direta."
    )
    sends_before_runner = len(adapter.sent_messages)

    result = browser.run_writing(project_id)

    assert result["stop_reason"] == "complete"
    recovered = result["recovered_interactions"]
    assert isinstance(recovered, list) and len(recovered) == 1
    assert result["accepted_units"] == ["START", "END"]
    assert len(adapter.sent_messages) == sends_before_runner + 2


def test_writing_runner_auto_confirms_warning_review_and_processes_next_unit(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SequencedAdapter(FakeChatAdapter):
        def __init__(self) -> None:
            super().__init__()
            self.responses: list[str] = []

        def send_message(
            self,
            text: str,
            *,
            on_send_attempt_started: Callable[[], None] | None = None,
        ) -> str | None:
            if self.responses:
                self.next_response = self.responses.pop(0)
            return super().send_message(
                text, on_send_attempt_started=on_send_attempt_started
            )

    adapter = SequencedAdapter()
    project_id, _context_id, browser = _confirmed_browser_context(
        services, tmp_path, project_config_path, repository_root, adapter
    )
    adapter.responses = [
        (
            "A abertura desenvolve conteúdo suficiente para validação.\n\n"
            "A análise continua com densidade acadêmica adequada."
        ),
        "Texto acadêmico com extensão objetiva suficiente para aceitação direta.",
    ]
    confirmed_raw_versions: list[tuple[str, int]] = []
    original_confirm = services.writing.confirm_unit

    def tracked_confirm(
        project: str, unit_id: str, raw_version: int
    ) -> TextUnitSubmission:
        confirmed_raw_versions.append((unit_id, raw_version))
        return original_confirm(project, unit_id, raw_version)

    monkeypatch.setattr(services.writing, "confirm_unit", tracked_confirm)

    result = browser.run_writing(project_id)

    assert result["stop_reason"] == "complete"
    assert result["processed_units"] == ["START", "END"]
    assert result["accepted_units"] == ["START", "END"]
    assert result["review_required_unit"] is None
    assert result["warning_count"] == 2
    assert result["warning_units"] == [
        {
            "unit_id": "START",
            "warning_count": 2,
            "codes": [
                "WRITING_REPEATED_FIRST_WORD",
                "WRITING_REPEATED_ARTICLE_OPENING",
            ],
        }
    ]
    assert confirmed_raw_versions == [("START", 1)]
    assert len(adapter.sent_messages) == 3


def test_writing_runner_accumulates_consecutive_warning_units_without_pause(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    adapter = FakeChatAdapter()
    project_id, _context_id, browser = _confirmed_browser_context(
        services, tmp_path, project_config_path, repository_root, adapter
    )
    adapter.next_response = (
        "A abertura desenvolve conteúdo suficiente para validação.\n\n"
        "A análise continua com densidade acadêmica adequada."
    )

    result = browser.run_writing(project_id)

    assert result["stop_reason"] == "complete"
    assert result["processed_units"] == ["START", "END"]
    assert result["accepted_units"] == ["START", "END"]
    assert result["warning_count"] == 4
    assert result["warning_units"] == [
        {
            "unit_id": "START",
            "warning_count": 2,
            "codes": [
                "WRITING_REPEATED_FIRST_WORD",
                "WRITING_REPEATED_ARTICLE_OPENING",
            ],
        },
        {
            "unit_id": "END",
            "warning_count": 2,
            "codes": [
                "WRITING_REPEATED_FIRST_WORD",
                "WRITING_REPEATED_ARTICLE_OPENING",
            ],
        },
    ]
    assert len(adapter.sent_messages) == 3


def test_writing_runner_does_not_confirm_rejected_submission(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = FakeChatAdapter()
    project_id, context_id, browser = _confirmed_browser_context(
        services, tmp_path, project_config_path, repository_root, adapter
    )
    adapter.next_response = "Curto."

    def forbidden_confirm(_project: str, _unit_id: str, _raw_version: int) -> Never:
        raise AssertionError("rejected submission must not be confirmed")

    monkeypatch.setattr(services.writing, "confirm_unit", forbidden_confirm)

    result = browser.run_writing(project_id)

    assert result["stop_reason"] == "rejected"
    assert result["processed_units"] == ["START"]
    assert result["accepted_units"] == []
    assert result["warning_count"] == 0
    assert len(adapter.sent_messages) == 2
    with services.database.connection() as connection:
        rejected = SubmissionRepository(connection).latest(context_id, "START")
    assert rejected is not None
    error_count = services.writing.unit_validation_report(project_id, rejected)["error_count"]
    assert isinstance(error_count, int)
    assert error_count > 0


def test_writing_runner_auto_confirm_failure_is_fail_closed(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = FakeChatAdapter()
    project_id, _context_id, browser = _confirmed_browser_context(
        services, tmp_path, project_config_path, repository_root, adapter
    )
    adapter.next_response = (
        "A abertura desenvolve conteúdo suficiente para validação.\n\n"
        "A análise continua com densidade acadêmica adequada."
    )

    def failed_confirm(_project: str, _unit_id: str, _raw_version: int) -> Never:
        raise IntegrityError(
            "WRITING_SYNTHETIC_CONFIRM_FAILURE",
            "Synthetic confirmation failure",
        )

    monkeypatch.setattr(services.writing, "confirm_unit", failed_confirm)

    result = browser.run_writing(project_id)

    assert result["stop_reason"] == "blocked"
    assert result["error_code"] == "WRITING_SYNTHETIC_CONFIRM_FAILURE"
    assert result["processed_units"] == ["START"]
    assert result["accepted_units"] == []
    assert result["warning_count"] == 2
    assert result["blocked_interaction"] is not None
    assert len(adapter.sent_messages) == 2


def test_writing_runner_stops_at_unresolved_pre_send_block(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    adapter = FakeChatAdapter()
    project_id, context_id, browser = _confirmed_browser_context(
        services, tmp_path, project_config_path, repository_root, adapter
    )
    adapter.session_state = SessionState.LOGIN_REQUIRED
    with pytest.raises(IntegrityError) as captured:
        browser.run_unit(project_id, "START", context_id=context_id)
    assert captured.value.code == "BROWSER_LOGIN_REQUIRED"
    sends_before_runner = len(adapter.sent_messages)
    adapter.session_state = SessionState.READY
    with services.database.connection() as connection:
        blocked = BrowserInteractionRepository(connection).latest_for_context_kind(
            context_id, InteractionKind.UNIT_REQUEST, "START"
        )
    assert blocked is not None

    result = browser.run_writing(project_id)

    assert result["stop_reason"] == "blocked"
    assert result["blocked_interaction"] == blocked.id
    assert result["processed_units"] == []
    assert len(adapter.sent_messages) == sends_before_runner


def test_unit_send_persists_local_tail_anchor_before_effect_boundary(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    adapter = FakeChatAdapter()
    project_id, context_id, browser = _confirmed_browser_context(
        services, tmp_path, project_config_path, repository_root, adapter
    )
    adapter.next_response = (
        "Texto acadêmico com extensão objetiva suficiente para aceitação direta."
    )

    result = browser.run_unit(project_id, "START", context_id=context_id)

    with services.database.connection() as connection:
        events = BrowserInteractionRepository(connection).events(str(result["id"]))
    sending = next(event for event in events if event["status"] == "sending")
    evidence = sending["evidence"]
    assert isinstance(evidence, dict)
    anchor = evidence["pre_send_turn_anchor"]
    assert isinstance(anchor, dict)
    assert anchor["conversation_path"] == DEFAULT_CONVERSATION_PATH
    assert anchor["pre_send_user_turn_count"] == 1
    context_fingerprint = next(iter(adapter.conversations[DEFAULT_CONVERSATION_PATH]))
    assert anchor["tail"] == [
        {
            "role": "user",
            "id": f"user-{context_fingerprint}",
        },
        {
            "role": "assistant",
            "id": f"assistant-{context_fingerprint}",
        },
    ]


def test_unit_send_fails_before_effect_when_local_tail_anchor_is_missing(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    class MissingAnchorAdapter(FakeChatAdapter):
        require_anchor = False

        def pre_send_turn_anchor(self) -> dict[str, object] | None:
            return None if self.require_anchor else super().pre_send_turn_anchor()

    adapter = MissingAnchorAdapter()
    project_id, context_id, browser = _confirmed_browser_context(
        services, tmp_path, project_config_path, repository_root, adapter
    )
    adapter.require_anchor = True
    sends_before = tuple(adapter.sent_messages)

    with pytest.raises(ConflictError) as captured:
        browser.run_unit(project_id, "START", context_id=context_id)

    assert captured.value.code == "BROWSER_PRE_SEND_TURN_ANCHOR_UNAVAILABLE"
    assert tuple(adapter.sent_messages) == sends_before
    with services.database.connection() as connection:
        interaction = BrowserInteractionRepository(connection).latest_for_context_kind(
            context_id, InteractionKind.UNIT_REQUEST, "START"
        )
        assert interaction is not None
        events = BrowserInteractionRepository(connection).events(interaction.id)
    assert interaction.status is InteractionStatus.PREPARED
    assert [event["status"] for event in events] == ["prepared"]


def test_bootstrap_recovery_after_send_binds_one_new_candidate_without_resend(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project_id, context_id = _automation_context(
        services, tmp_path, project_config_path, repository_root
    )
    adapter = FakeChatAdapter()
    crashing = _service(services, adapter, "browser.after_send")
    with pytest.raises(RuntimeError, match="browser.after_send"):
        crashing.run_context(project_id, context_id)
    assert len(adapter.sent_messages) == 1
    with services.database.connection() as connection:
        interaction = BrowserInteractionRepository(connection).latest_for_context_kind(
            context_id, kind=InteractionKind.CONTEXT_LOAD
        )
        assert interaction is not None and interaction.status is InteractionStatus.SENDING
        conversation = BrowserConversationRepository(connection).get(interaction.conversation_id)
        assert conversation.conversation_path is None
    restarted = _service(services, adapter)
    recovered = BrowserRecovery(restarted).recover(project_id)
    assert _recovered_items(recovered)[0]["status"] == "imported"
    assert len(adapter.sent_messages) == 1


def test_resumed_sending_without_recoverable_conversation_requires_reconcile(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project_id, context_id = _automation_context(
        services, tmp_path, project_config_path, repository_root
    )
    original_adapter = FakeChatAdapter()
    with pytest.raises(RuntimeError, match="browser.after_send"):
        _service(services, original_adapter, "browser.after_send").run_context(
            project_id, context_id
        )
    resumed_adapter = FakeChatAdapter()

    with pytest.raises(IntegrityError) as captured:
        _service(services, resumed_adapter).run_context(project_id, context_id)

    assert captured.value.code == "BROWSER_SEND_REQUIRES_RECONCILE"
    assert resumed_adapter.sent_messages == []


def test_normal_run_uses_only_structural_post_send_proof(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project_id, context_id = _automation_context(
        services, tmp_path, project_config_path, repository_root
    )

    class StructuralOnlyAdapter(FakeChatAdapter):
        def inspect_turn(self, fingerprint: str) -> TurnInspection:
            del fingerprint
            raise AssertionError("integral post-Send proof must not run on the happy path")

        def capture_response(self, fingerprint: str) -> str:
            del fingerprint
            raise AssertionError("integral response lookup must not run on the happy path")

    adapter = StructuralOnlyAdapter()

    result = _service(services, adapter).run_context(project_id, context_id)

    assert result["status"] == "imported"
    assert len(adapter.sent_messages) == 1


def test_send_waits_for_delayed_user_turn_before_marking_sent(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    project_id, context_id = _automation_context(
        services, tmp_path, project_config_path, repository_root
    )

    class DelayedTurnAdapter(FakeChatAdapter):
        def __init__(self) -> None:
            super().__init__()
            self.inspections = 0

        def inspect_sent_turn_structure(self, fingerprint: str) -> TurnInspection:
            self.inspections += 1
            if self.inspections <= 12:
                return TurnInspection(TurnState.NOT_SENT, self.current_path)
            return super().inspect_sent_turn_structure(fingerprint)

    clock = {"now": 0.0}

    def advance(seconds: float) -> None:
        clock["now"] += seconds

    monkeypatch.setattr(browser_service_module, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(browser_service_module, "sleep", advance)
    adapter = DelayedTurnAdapter()
    with caplog.at_level("DEBUG", logger="ebook_pipeline.browser.service"):
        result = _service(services, adapter).run_context(project_id, context_id)

    assert result["status"] == "imported"
    assert adapter.inspections == 13
    assert clock["now"] == 3.0
    assert len(adapter.sent_messages) == 1
    with services.database.connection() as connection:
        interaction = BrowserInteractionRepository(connection).latest_for_context_kind(
            context_id, InteractionKind.CONTEXT_LOAD
        )
        assert interaction is not None and interaction.sent_at is not None
        conversation = BrowserConversationRepository(connection).get(
            interaction.conversation_id
        )
        events = BrowserInteractionRepository(connection).events(interaction.id)
    assert conversation.conversation_path == DEFAULT_CONVERSATION_PATH
    assert conversation.first_turn_fingerprint == interaction.transport_fingerprint
    assert [event["status"] for event in events] == [
        "prepared",
        "sending",
        "sent",
        "captured",
        "imported",
    ]
    assert events[0]["evidence"] == {"effect_boundary": "pre_send"}
    assert events[1]["evidence"] == {"effect_boundary": "send_attempt_started"}
    sent_evidence = events[2]["evidence"]
    assert isinstance(sent_evidence, dict) and sent_evidence["user_turn_observed"] is True
    assert sent_evidence["effect_boundary"] == "send_observed"
    assert sent_evidence["observed_user_turn_fingerprint"] == interaction.transport_fingerprint
    assert any(
        getattr(record, "status", None) == "user_turn_observed"
        and getattr(record, "operation", None) == "browser.send_message"
        for record in caplog.records
    )


@pytest.mark.parametrize(
    ("invalid_path", "inspection_state", "expected_code"),
    [
        ("/c/WEB:synthetic", None, "BROWSER_CONVERSATION_PATH_INVALID"),
        (DEFAULT_CONVERSATION_PATH, TurnState.NOT_SENT, "BROWSER_SEND_NOT_PROVABLE"),
    ],
)
def test_response_capture_never_precedes_structural_user_turn_and_canonical_url_proof(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
    invalid_path: str,
    inspection_state: TurnState | None,
    expected_code: str,
) -> None:
    project_id, context_id = _automation_context(
        services, tmp_path, project_config_path, repository_root
    )

    class CaptureGuardAdapter(FakeChatAdapter):
        def __init__(self) -> None:
            super().__init__()
            self.capture_calls = 0

        def capture_structural_response(self, fingerprint: str) -> str:
            self.capture_calls += 1
            return super().capture_structural_response(fingerprint)

    adapter = CaptureGuardAdapter()
    adapter.next_path = invalid_path
    adapter.inspect_override = inspection_state
    zero_timeout_config = services.config.model_copy(
        update={"browser_timeout_seconds": 0}
    )
    browser = BrowserAutomationService(
        zero_timeout_config,
        services.database,
        services.store,
        adapter,
        services.writing,
    )

    with pytest.raises(IntegrityError) as captured:
        browser.run_context(project_id, context_id)

    assert captured.value.code == expected_code
    assert adapter.capture_calls == 0


def test_fill_failure_before_send_attempt_is_retry_safe_and_keeps_pre_send_boundary(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project_id, context_id = _automation_context(
        services, tmp_path, project_config_path, repository_root
    )

    class DraftNeverSentAdapter(FakeChatAdapter):
        def send_message(
            self,
            text: str,
            *,
            on_send_attempt_started: Callable[[], None] | None = None,
        ) -> str | None:
            del on_send_attempt_started
            assert text
            assert self.current_path is None  # page.url remains the New chat root.
            raise ConflictError(
                "BROWSER_SEND_NOT_TRIGGERED",
                "The draft remained in the composer",
            )

    adapter = DraftNeverSentAdapter()
    browser = _service(services, adapter)
    with pytest.raises(ConflictError) as captured:
        browser.run_context(project_id, context_id)
    assert captured.value.code == "BROWSER_SEND_NOT_TRIGGERED"

    with services.database.connection() as connection:
        interaction = BrowserInteractionRepository(connection).latest_for_context_kind(
            context_id, InteractionKind.CONTEXT_LOAD
        )
        assert interaction is not None
        conversation = BrowserConversationRepository(connection).get(
            interaction.conversation_id
        )
        events = BrowserInteractionRepository(connection).events(interaction.id)
        run = connection.execute(
            "SELECT status, attempt, started_at FROM stage_runs WHERE id = ?",
            (interaction.stage_run_id,),
        ).fetchone()
    assert interaction.status is InteractionStatus.PREPARED
    assert interaction.attempt == 0
    assert interaction.sent_at is None
    assert conversation.status is ConversationStatus.PROVISIONING
    assert conversation.conversation_path is None
    assert conversation.first_turn_fingerprint is None
    assert [event["status"] for event in events] == ["prepared"]
    assert events[0]["evidence"] == {"effect_boundary": "pre_send"}
    assert run is not None
    assert tuple(run) == ("pending", 0, None)
    assert adapter.sent_messages == []

    retry_adapter = FakeChatAdapter()
    result = _service(services, retry_adapter).run_context(project_id, context_id)
    assert result["id"] == interaction.id
    assert result["status"] == "imported"
    assert len(retry_adapter.sent_messages) == 1


def test_abandoned_legacy_web_conversation_is_superseded_without_rewriting_history(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id, context_id = _automation_context(
        services, tmp_path, project_config_path, repository_root
    )
    adapter = FakeChatAdapter()
    browser = _service(services, adapter)
    prepared = browser.prepare_context(project_id, context_id)
    invalid_path = "/c/WEB:9491397d-073e-4cc8-a1b9-a38a8968cfaf"
    with (
        services.database.connection() as connection,
        services.database.transaction(connection),
    ):
        interaction = browser.persistence.begin_send(connection, prepared)
        conversations = BrowserConversationRepository(connection)
        conversation = conversations.get(interaction.conversation_id)
        now = utc_now()
        legacy_conversation = replace(
            conversation,
            status=ConversationStatus.READY,
            conversation_path=invalid_path,
            first_turn_fingerprint=interaction.transport_fingerprint,
            ready_at=now,
            updated_at=now,
        )
        conversations.update(legacy_conversation, expected_status=conversation.status)
        legacy_sent = browser.persistence.interaction_states.transition(
            interaction, InteractionStatus.SENT
        )
        interactions = BrowserInteractionRepository(connection)
        interactions.update(legacy_sent, expected_status=interaction.status)
        interactions.event(
            interaction.id,
            interaction.project_id,
            interaction.status,
            legacy_sent.status,
            {"legacy_path_only_transition": True},
        )
        interaction = legacy_sent
    with pytest.raises(IntegrityError):
        browser._block(  # noqa: SLF001 - constructs an immutable historical regression.
            interaction,
            "BROWSER_SEND_NOT_PROVABLE",
            "Legacy path-only proof was invalid",
        )
    with services.database.connection() as connection:
        events_before = BrowserInteractionRepository(connection).events(interaction.id)

    original_open = adapter.open_conversation
    monkeypatch.setattr(
        adapter,
        "open_conversation",
        lambda _path: (_ for _ in ()).throw(AssertionError("invalid path must not be opened")),
    )
    report = BrowserRecovery(browser).recover(project_id)
    assert _recovered_items(report) == []
    assert _skipped_items(report)[0]["reason"] == "invalid_conversation_path"
    with services.database.connection() as connection:
        current = BrowserInteractionRepository(connection).get(interaction.id)
        current_conversation = BrowserConversationRepository(connection).get(
            interaction.conversation_id
        )
        events_after = BrowserInteractionRepository(connection).events(interaction.id)
    assert current.status is InteractionStatus.BLOCKED
    assert current_conversation.conversation_path == invalid_path
    assert events_after == events_before
    assert any(
        issue.code == "BROWSER_CONVERSATION_PATH_INVALID"
        for issue in browser.validate(project_id)
    )
    with pytest.raises(ConflictError) as unresolved:
        browser.prepare_context(project_id, context_id)
    assert unresolved.value.code == "BROWSER_INVALID_CONVERSATION_REQUIRES_RESOLUTION"

    browser.abandon_interaction(
        project_id,
        interaction.id,
        operator="operator-test",
        reason="legacy send could not be proven",
    )
    with (
        services.database.connection() as connection,
        services.database.transaction(connection),
    ):
        second = browser.persistence.create_interaction(
            connection,
            project_id=interaction.project_id,
            conversation_id=interaction.conversation_id,
            context_id=interaction.context_id,
            kind=interaction.kind,
            request_artifact_id=interaction.request_artifact_id,
            request_sha256=interaction.request_sha256,
            transport_fingerprint=interaction.transport_fingerprint,
        )
        second = browser.persistence.begin_send(connection, second)
        second_sent = browser.persistence.interaction_states.transition(
            second, InteractionStatus.SENT
        )
        interactions = BrowserInteractionRepository(connection)
        interactions.update(second_sent, expected_status=second.status)
        interactions.event(
            second.id,
            second.project_id,
            second.status,
            second_sent.status,
            {"legacy_path_only_transition": True},
        )
    with pytest.raises(IntegrityError):
        browser._block(  # noqa: SLF001 - second immutable historical attempt.
            second_sent,
            "BROWSER_SEND_NOT_PROVABLE",
            "Second legacy path-only proof was invalid",
        )
    browser.abandon_interaction(
        project_id,
        second.id,
        operator="operator-test",
        reason="second legacy send could not be proven",
    )
    with services.database.connection() as connection:
        old_conversation = BrowserConversationRepository(connection).get(
            interaction.conversation_id
        )
        first_events = BrowserInteractionRepository(connection).events(interaction.id)
        second_events = BrowserInteractionRepository(connection).events(second.id)

    monkeypatch.setattr(adapter, "open_conversation", original_open)
    result = browser.run_context(project_id, context_id)
    assert result["status"] == "imported"
    assert len(adapter.sent_messages) == 1

    with services.database.connection() as connection:
        conversation_history = BrowserConversationRepository(connection).list_for_context(
            context_id
        )
        invalidation = BrowserConversationInvalidationRepository(
            connection
        ).for_conversation(old_conversation.id)
        preserved_old = BrowserConversationRepository(connection).get(old_conversation.id)
        assert BrowserInteractionRepository(connection).events(interaction.id) == first_events
        assert BrowserInteractionRepository(connection).events(second.id) == second_events
    assert len(conversation_history) == 2
    assert preserved_old == old_conversation
    assert invalidation is not None
    assert invalidation.reason == "legacy_invalid_conversation_path"
    replacement = next(
        item for item in conversation_history if item.id != old_conversation.id
    )
    assert invalidation.replacement_conversation_id == replacement.id
    assert replacement.status is ConversationStatus.READY
    assert replacement.conversation_path == DEFAULT_CONVERSATION_PATH
    assert not any(
        issue.code == "BROWSER_CONVERSATION_PATH_INVALID"
        for issue in browser.validate(project_id)
    )
    status = browser.status(project_id)
    status_conversations = status["conversations"]
    assert isinstance(status_conversations, list)
    conversation_statuses = {
        str(item["id"]): item for item in status_conversations if isinstance(item, dict)
    }
    assert conversation_statuses[old_conversation.id]["reusable"] is False
    assert conversation_statuses[old_conversation.id]["invalidation"]["reason"] == (
        "legacy_invalid_conversation_path"
    )
    assert conversation_statuses[replacement.id]["reusable"] is True


def test_blocked_send_not_provable_is_reprobed_after_restart_without_resend(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id, context_id = _automation_context(
        services, tmp_path, project_config_path, repository_root
    )
    adapter = FakeChatAdapter()
    adapter.inspect_override = TurnState.STREAMING
    interrupted = _service(services, adapter, "browser.streaming")
    with pytest.raises(RuntimeError, match="browser.streaming"):
        interrupted.run_context(project_id, context_id)
    assert len(adapter.sent_messages) == 1

    with (
        services.database.connection() as connection,
        services.database.transaction(connection),
    ):
        interaction = BrowserInteractionRepository(connection).latest_for_context_kind(
            context_id, InteractionKind.CONTEXT_LOAD
        )
        assert interaction is not None
        conversation = BrowserConversationRepository(connection).get(
            interaction.conversation_id
        )
        assert interaction.status is InteractionStatus.STREAMING
        assert interaction.sent_at is not None
        assert conversation.status.value == "ready"
        assert conversation.conversation_path == DEFAULT_CONVERSATION_PATH
        legacy_sent = interaction
    assert legacy_sent.sent_at is not None
    with pytest.raises(IntegrityError) as blocked:
        interrupted._block(  # noqa: SLF001 - regression reproduces historical persisted state.
            legacy_sent,
            "BROWSER_SEND_NOT_PROVABLE",
            "Historical immediate inspection could not prove the user turn",
        )
    assert blocked.value.code == "BROWSER_SEND_NOT_PROVABLE"

    def forbidden_send(_text: str) -> str | None:
        raise AssertionError("recovery must not call send_message")

    monkeypatch.setattr(adapter, "send_message", forbidden_send)

    adapter.inspect_override = TurnState.NOT_SENT
    zero_timeout = services.config.model_copy(update={"browser_timeout_seconds": 0})
    unresolved_service = BrowserAutomationService(
        zero_timeout,
        services.database,
        services.store,
        adapter,
        services.writing,
    )
    unresolved = BrowserRecovery(unresolved_service).recover(project_id)
    assert _recovered_items(unresolved) == []
    assert _skipped_items(unresolved)[0]["reason"] == "user_turn_not_observed"
    with services.database.connection() as connection:
        assert (
            BrowserInteractionRepository(connection).get(legacy_sent.id).status
            is InteractionStatus.BLOCKED
        )
    assert len(adapter.sent_messages) == 1

    adapter.inspect_override = None
    restarted = _service(services, adapter)
    report = BrowserRecovery(restarted).recover(project_id)
    recovered = _recovered_items(report)
    assert recovered[0]["status"] == "imported"
    assert _skipped_items(report) == []
    assert report["examined"] == [
        {
            "interaction_id": legacy_sent.id,
            "status": "blocked",
            "kind": "context_load",
            "unit_id": None,
        }
    ]
    assert len(adapter.sent_messages) == 1
    with services.database.connection() as connection:
        final = BrowserInteractionRepository(connection).get(legacy_sent.id)
        events = BrowserInteractionRepository(connection).events(legacy_sent.id)
        run = connection.execute(
            "SELECT status, attempt FROM stage_runs WHERE id = ?", (final.stage_run_id,)
        ).fetchone()
    assert final.status is InteractionStatus.IMPORTED
    assert final.sent_at == legacy_sent.sent_at
    reprobe_event_found = False
    for event in events:
        evidence = event["evidence"]
        if (
            event["previous_status"] == "blocked"
            and event["status"] == "sent"
            and isinstance(evidence, dict)
            and evidence.get("recovery_reprobe") is True
        ):
            reprobe_event_found = True
    assert reprobe_event_found
    assert run is not None and tuple(run) == ("done", 1)


def test_explicit_reconcile_proves_current_conversation_and_imports_same_interaction(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project_id, context_id, adapter, browser = _blocked_unprovable_context(
        services, tmp_path, project_config_path, repository_root
    )
    with services.database.connection() as connection:
        before = BrowserInteractionRepository(connection).latest_for_context_kind(
            context_id, InteractionKind.CONTEXT_LOAD
        )
        assert before is not None
        attempt_before = before.attempt
        sent_count_before = len(adapter.sent_messages)

    result = browser.reconcile_blocked_interaction(project_id, before.id)

    assert result["outcome"] == "reconciled"
    assert result["interaction_id"] == before.id
    assert len(adapter.sent_messages) == sent_count_before
    with services.database.connection() as connection:
        after = BrowserInteractionRepository(connection).get(before.id)
        conversation = BrowserConversationRepository(connection).get(after.conversation_id)
        interactions = BrowserInteractionRepository(connection).list_for_project(project_id)
        run = connection.execute(
            "SELECT status FROM stage_runs WHERE id = ?", (after.stage_run_id,)
        ).fetchone()
        acknowledgement = AcknowledgementRepository(connection).latest(context_id)
    assert after.status is InteractionStatus.IMPORTED
    assert after.attempt == attempt_before
    assert after.response_artifact_id is not None
    assert after.response_sha256 is not None
    assert after.capture_method_version == "rendered_text_v1"
    assert after.sent_at is not None
    assert conversation.status is ConversationStatus.READY
    assert conversation.conversation_path == DEFAULT_CONVERSATION_PATH
    assert conversation.first_turn_fingerprint == before.transport_fingerprint
    assert len(interactions) == 1
    assert run is not None and run["status"] == "done"
    assert acknowledgement is not None
    assert acknowledgement.context_id == context_id


def test_reconciliation_required_error_after_send_boundary_blocks_atomically(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id, context_id = _automation_context(
        services, tmp_path, project_config_path, repository_root
    )
    adapter = FakeChatAdapter()
    browser = _service(services, adapter)

    def requires_reconcile(_fingerprint: str) -> TurnInspection:
        raise ConflictError(
            "BROWSER_SEND_REQUIRES_RECONCILE",
            "The in-process pre-Send user-turn baseline is unavailable",
        )

    monkeypatch.setattr(adapter, "inspect_sent_turn_structure", requires_reconcile)

    with pytest.raises(ConflictError) as captured:
        browser.run_context(project_id, context_id)

    assert captured.value.code == "BROWSER_SEND_REQUIRES_RECONCILE"
    assert len(adapter.sent_messages) == 1
    with services.database.connection() as connection:
        interaction = BrowserInteractionRepository(connection).latest_for_context_kind(
            context_id, InteractionKind.CONTEXT_LOAD
        )
        assert interaction is not None
        events = BrowserInteractionRepository(connection).events(interaction.id)
        run = connection.execute(
            "SELECT status, finished_at FROM stage_runs WHERE id = ?",
            (interaction.stage_run_id,),
        ).fetchone()
    assert interaction.status is InteractionStatus.BLOCKED
    assert [event["status"] for event in events] == ["prepared", "sending", "blocked"]
    assert events[1]["evidence"] == {"effect_boundary": "send_attempt_started"}
    assert events[2]["evidence"] == {
        "error_code": "BROWSER_SEND_REQUIRES_RECONCILE"
    }
    assert run is not None and run["status"] == "blocked"
    assert run["finished_at"] is not None

    with pytest.raises(ConflictError) as repeated:
        browser.run_interaction(interaction.id)
    assert repeated.value.code == "BROWSER_INTERACTION_BLOCKED"
    with services.database.connection() as connection:
        repeated_events = BrowserInteractionRepository(connection).events(interaction.id)
    assert repeated_events == events


def test_reconciliation_required_error_from_send_call_blocks_after_persisted_boundary(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id, context_id = _automation_context(
        services, tmp_path, project_config_path, repository_root
    )
    adapter = FakeChatAdapter()
    browser = _service(services, adapter)

    def interrupted_after_boundary(
        _text: str,
        *,
        on_send_attempt_started: Callable[[], None] | None = None,
    ) -> str | None:
        assert on_send_attempt_started is not None
        on_send_attempt_started()
        raise ConflictError(
            "BROWSER_SEND_REQUIRES_RECONCILE",
            "Post-Send evidence requires explicit reconciliation",
        )

    monkeypatch.setattr(adapter, "send_message", interrupted_after_boundary)

    with pytest.raises(ConflictError) as captured:
        browser.run_context(project_id, context_id)

    assert captured.value.code == "BROWSER_SEND_REQUIRES_RECONCILE"
    with services.database.connection() as connection:
        interaction = BrowserInteractionRepository(connection).latest_for_context_kind(
            context_id, InteractionKind.CONTEXT_LOAD
        )
        assert interaction is not None
        events = BrowserInteractionRepository(connection).events(interaction.id)
        run = connection.execute(
            "SELECT status, finished_at FROM stage_runs WHERE id = ?",
            (interaction.stage_run_id,),
        ).fetchone()
    assert interaction.status is InteractionStatus.BLOCKED
    assert events[-1]["status"] == "blocked"
    assert events[-1]["evidence"] == {
        "error_code": "BROWSER_SEND_REQUIRES_RECONCILE"
    }
    assert run is not None and run["status"] == "blocked"
    assert run["finished_at"] is not None


def test_explicit_orphaned_send_block_is_idempotent_and_enables_reconcile(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project_id, context_id = _automation_context(
        services, tmp_path, project_config_path, repository_root
    )
    adapter = FakeChatAdapter()
    browser = _service(services, adapter)
    prepared = browser.prepare_context(project_id, context_id)
    with (
        services.database.connection() as connection,
        services.database.transaction(connection),
    ):
        conversation = BrowserConversationRepository(connection).get(
            prepared.conversation_id
        )
        browser.persistence.record_provisioning_baseline(connection, conversation, ())
        sending = browser.persistence.begin_send(connection, prepared)
    with services.database.connection() as connection:
        events_before = BrowserInteractionRepository(connection).events(sending.id)

    first = browser.block_orphaned_sending_interaction(project_id, sending.id)

    assert first == {
        "outcome": "blocked",
        "interaction_id": sending.id,
        "status": "blocked",
        "error_code": "BROWSER_SEND_REQUIRES_RECONCILE",
    }
    assert adapter.sent_messages == []
    with services.database.connection() as connection:
        blocked = BrowserInteractionRepository(connection).get(sending.id)
        events_after = BrowserInteractionRepository(connection).events(sending.id)
        run = connection.execute(
            "SELECT status, finished_at FROM stage_runs WHERE id = ?",
            (sending.stage_run_id,),
        ).fetchone()
    assert blocked.status is InteractionStatus.BLOCKED
    assert events_after[:-1] == events_before
    assert events_after[-1]["status"] == "blocked"
    assert events_after[-1]["evidence"] == {
        "error_code": "BROWSER_SEND_REQUIRES_RECONCILE",
        "resolution": "orphaned_sending_to_blocked",
    }
    assert run is not None and run["status"] == "blocked"
    assert run["finished_at"] is not None

    repeated = browser.block_orphaned_sending_interaction(project_id, sending.id)
    assert repeated["outcome"] == "already_blocked"
    with services.database.connection() as connection:
        assert BrowserInteractionRepository(connection).events(sending.id) == events_after

    adapter.current_path = DEFAULT_CONVERSATION_PATH
    adapter.conversations[DEFAULT_CONVERSATION_PATH] = {
        sending.transport_fingerprint: adapter.next_response
    }
    reconciled = browser.reconcile_blocked_interaction(project_id, sending.id)

    assert reconciled["outcome"] == "reconciled"
    assert adapter.sent_messages == []


def test_explicit_reconcile_imports_blocked_unit_by_exact_persisted_preparation(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project_id, context_id = _automation_context(
        services, tmp_path, project_config_path, repository_root
    )
    adapter = FakeChatAdapter()
    browser = _service(services, adapter)
    context_result = browser.run_context(project_id, context_id)
    assert context_result["status"] == "imported"
    with services.database.connection() as connection:
        acknowledgement = AcknowledgementRepository(connection).latest(context_id)
    assert acknowledgement is not None
    services.writing.confirm_context_by_id(
        project_id, context_id, acknowledgement.raw_version
    )
    prepared = browser.prepare_unit(project_id, "START", context_id=context_id)
    with (
        services.database.connection() as connection,
        services.database.transaction(connection),
    ):
        sending = browser.persistence.begin_send(
            connection,
            prepared,
            pre_send_turn_anchor=_persisted_unit_turn_anchor(),
        )
    browser.block_orphaned_sending_interaction(project_id, sending.id)
    response = (
        "A abertura desenvolve conteúdo suficiente para validação.\n\n"
        "A análise continua com densidade acadêmica adequada."
    )
    adapter.current_path = DEFAULT_CONVERSATION_PATH
    adapter.conversations[DEFAULT_CONVERSATION_PATH][sending.transport_fingerprint] = (
        response
    )
    sent_count_before = len(adapter.sent_messages)

    reconciled = browser.reconcile_blocked_interaction(project_id, sending.id)

    assert reconciled["outcome"] == "reconciled"
    assert reconciled["user_turn_ordinal"] == 1
    assert adapter.reconciliation_bindings
    assert all(isinstance(binding, dict) for binding in adapter.reconciliation_bindings)
    result = reconciled["result"]
    assert isinstance(result, dict)
    assert result["status"] == "imported"
    assert result["disposition"] in {"accepted", "review_required", "rejected"}
    assert len(adapter.sent_messages) == sent_count_before
    with services.database.connection() as connection:
        imported = BrowserInteractionRepository(connection).get(sending.id)
        unit_interactions = [
            candidate
            for candidate in BrowserInteractionRepository(connection).list_for_conversation(
                sending.conversation_id
            )
            if candidate.kind is InteractionKind.UNIT_REQUEST
        ]
        run = connection.execute(
            "SELECT status, finished_at FROM stage_runs WHERE id = ?",
            (sending.stage_run_id,),
        ).fetchone()
    assert imported.status is InteractionStatus.IMPORTED
    assert imported.preparation_id == prepared.preparation_id
    assert imported.response_artifact_id is not None
    assert len(unit_interactions) == 1
    assert run is not None and run["status"] == "done"
    assert run["finished_at"] is not None


@pytest.mark.parametrize(
    ("scenario", "expected_reload_count", "expected_error_code"),
    [
        ("reload_recovers", 1, None),
        ("hydrates_without_reload", 0, None),
        (
            "reload_stays_incomplete",
            1,
            "BROWSER_RECONCILE_UNIT_TURN_HYDRATION_TIMEOUT",
        ),
        ("ambiguous", 0, "BROWSER_RECONCILE_UNIT_TURN_AMBIGUOUS"),
    ],
)
def test_unit_reconcile_uses_at_most_one_read_only_hydration_reload(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
    expected_reload_count: int,
    expected_error_code: str | None,
) -> None:
    project_id, context_id = _automation_context(
        services, tmp_path, project_config_path, repository_root
    )
    adapter = FakeChatAdapter()
    browser = _service(services, adapter)
    browser.run_context(project_id, context_id)
    with services.database.connection() as connection:
        acknowledgement = AcknowledgementRepository(connection).latest(context_id)
    assert acknowledgement is not None
    services.writing.confirm_context_by_id(
        project_id, context_id, acknowledgement.raw_version
    )
    prepared = browser.prepare_unit(project_id, "START", context_id=context_id)
    with (
        services.database.connection() as connection,
        services.database.transaction(connection),
    ):
        sending = browser.persistence.begin_send(
            connection,
            prepared,
            pre_send_turn_anchor=_persisted_unit_turn_anchor(3),
        )
    browser.block_orphaned_sending_interaction(project_id, sending.id)

    response = (
        "A abertura desenvolve conteúdo suficiente para validação.\n\n"
        "A análise continua com densidade acadêmica adequada."
    )
    empty_hydration = TurnInspection(
        TurnState.NOT_SENT,
        DEFAULT_CONVERSATION_PATH,
        evidence={
            "ordinal": 3,
            "user_count": 0,
            "assistant_count": 0,
            "expected_user_count": 4,
            "turn_count": 0,
            "failure_reason": "turn_cardinality_hydrating",
        },
    )
    hydration = TurnInspection(
        TurnState.NOT_SENT,
        DEFAULT_CONVERSATION_PATH,
        evidence={
            "ordinal": 3,
            "user_count": 3,
            "assistant_count": 3,
            "expected_user_count": 4,
            "turn_count": 6,
            "failure_reason": "turn_cardinality_hydrating",
        },
    )
    complete = TurnInspection(
        TurnState.COMPLETE,
        DEFAULT_CONVERSATION_PATH,
        response,
        evidence={
            "ordinal": 3,
            "user_count": 4,
            "assistant_count": 4,
            "expected_user_count": 4,
            "turn_count": 8,
            "failure_reason": "complete",
        },
    )
    ambiguous = TurnInspection(
        TurnState.AMBIGUOUS,
        DEFAULT_CONVERSATION_PATH,
        evidence={
            "ordinal": 3,
            "user_count": 4,
            "assistant_count": 5,
            "expected_user_count": 4,
            "turn_count": 9,
            "failure_reason": "unexplained_extra_assistant_turn",
        },
    )
    inspection_sequence = (
        [empty_hydration, hydration, hydration, complete]
        if scenario == "reload_recovers"
        else [hydration, complete]
        if scenario == "hydrates_without_reload"
        else []
    )

    def inspect(_ordinal: int, _path: str | None = None) -> TurnInspection:
        if scenario == "reload_stays_incomplete":
            return hydration
        if scenario == "ambiguous":
            return ambiguous
        return inspection_sequence.pop(0)

    monkeypatch.setattr(adapter, "inspect_reconciliation_unit_turn", inspect)
    monkeypatch.setattr(
        adapter, "capture_reconciled_unit_response", lambda _ordinal: response
    )
    opened_paths: list[str] = []
    original_open = adapter.open_conversation

    def tracked_open(conversation_path: str) -> None:
        opened_paths.append(conversation_path)
        original_open(conversation_path)

    monkeypatch.setattr(adapter, "open_conversation", tracked_open)
    monkeypatch.setattr(
        browser_service_module,
        "UNIT_RECONCILIATION_HYDRATION_STALL_SECONDS",
        0.0,
    )
    clock = {"now": 0.0}

    def advance(seconds: float) -> None:
        clock["now"] += seconds

    monkeypatch.setattr(browser_service_module, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(browser_service_module, "sleep", advance)
    if scenario == "reload_stays_incomplete":
        browser = BrowserAutomationService(
            services.config.model_copy(update={"browser_timeout_seconds": 1}),
            services.database,
            services.store,
            adapter,
            services.writing,
        )
    sent_before = tuple(adapter.sent_messages)

    if expected_error_code is None:
        result = browser.reconcile_blocked_interaction(project_id, sending.id)
        assert result["hydration_reload_count"] == expected_reload_count
        assert result["hydration_reload_triggered"] is bool(expected_reload_count)
        if expected_reload_count:
            assert result["pre_reload_counts"] == {
                "user_count": 3,
                "assistant_count": 3,
                "turn_count": 6,
            }
            assert result["post_reload_counts"] == {
                "user_count": 4,
                "assistant_count": 4,
                "turn_count": 8,
            }
    else:
        with pytest.raises(ConflictError) as captured:
            browser.reconcile_blocked_interaction(project_id, sending.id)
        assert captured.value.code == expected_error_code
        evidence = captured.value.context.evidence
        assert evidence is not None
        assert evidence["hydration_reload_count"] == expected_reload_count
    assert opened_paths == [DEFAULT_CONVERSATION_PATH]
    assert adapter.reloaded_paths == [DEFAULT_CONVERSATION_PATH] * expected_reload_count
    assert tuple(adapter.sent_messages) == sent_before


@pytest.mark.parametrize(
    ("failure_reason", "expected_error_code"),
    [
        ("completion_not_stable", "BROWSER_RECONCILE_UNIT_RESPONSE_INCOMPLETE"),
        (
            "turn_cardinality_hydrating",
            "BROWSER_RECONCILE_UNIT_TURN_HYDRATION_TIMEOUT",
        ),
    ],
)
def test_unit_reconcile_timeout_reports_last_shared_observation(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_reason: str,
    expected_error_code: str,
) -> None:
    project_id, context_id = _automation_context(
        services, tmp_path, project_config_path, repository_root
    )
    adapter = FakeChatAdapter()
    browser = _service(services, adapter)
    browser.run_context(project_id, context_id)
    with services.database.connection() as connection:
        acknowledgement = AcknowledgementRepository(connection).latest(context_id)
    assert acknowledgement is not None
    services.writing.confirm_context_by_id(
        project_id, context_id, acknowledgement.raw_version
    )
    prepared = browser.prepare_unit(project_id, "START", context_id=context_id)
    with (
        services.database.connection() as connection,
        services.database.transaction(connection),
    ):
        sending = browser.persistence.begin_send(
            connection,
            prepared,
            pre_send_turn_anchor=_persisted_unit_turn_anchor(),
        )
    browser.block_orphaned_sending_interaction(project_id, sending.id)
    expected_evidence: dict[str, object] = {
        "ordinal": 1,
        "user_count": 1 if failure_reason == "turn_cardinality_hydrating" else 2,
        "assistant_count": 1 if failure_reason == "turn_cardinality_hydrating" else 2,
        "selected_user_index": (
            None if failure_reason == "turn_cardinality_hydrating" else 2
        ),
        "selected_assistant_index": (
            None if failure_reason == "turn_cardinality_hydrating" else 3
        ),
        "semantic_container_count": (
            0 if failure_reason == "turn_cardinality_hydrating" else 1
        ),
        "semantic_visible_count": (
            0 if failure_reason == "turn_cardinality_hydrating" else 1
        ),
        "semantic_length": (
            0 if failure_reason == "turn_cardinality_hydrating" else 9780
        ),
        "semantic_sha_same": False,
        "no_generation_indicator": True,
        "no_stop": True,
        "post_response_control_count": (
            0 if failure_reason == "turn_cardinality_hydrating" else 3
        ),
        "stable_read_count": (
            0 if failure_reason == "turn_cardinality_hydrating" else 1
        ),
        "failure_reason": failure_reason,
    }
    monkeypatch.setattr(
        adapter,
        "inspect_reconciliation_unit_turn",
        lambda _ordinal, _path=None: TurnInspection(
            TurnState.STREAMING,
            adapter.current_path,
            evidence=expected_evidence,
        ),
    )
    timeout_config = services.config.model_copy(update={"browser_timeout_seconds": 0})
    timeout_browser = BrowserAutomationService(
        timeout_config,
        services.database,
        services.store,
        adapter,
        services.writing,
    )
    sent_before = tuple(adapter.sent_messages)

    with pytest.raises(ConflictError) as captured:
        timeout_browser.reconcile_blocked_interaction(project_id, sending.id)

    assert captured.value.code == expected_error_code
    evidence = captured.value.context.evidence
    assert evidence is not None
    assert {key: evidence[key] for key in expected_evidence} == expected_evidence
    assert isinstance(evidence["elapsed_ms"], int)
    assert evidence["elapsed_ms"] >= 0
    assert tuple(adapter.sent_messages) == sent_before


@pytest.mark.parametrize("source_status", ["imported", "captured"])
def test_observational_recapture_preserves_contaminated_v2_and_is_idempotent(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
    source_status: str,
) -> None:
    project_id, context_id = _automation_context(
        services, tmp_path, project_config_path, repository_root
    )
    adapter = FakeChatAdapter()
    browser = _service(services, adapter)
    browser.run_context(project_id, context_id)
    with services.database.connection() as connection:
        acknowledgement = AcknowledgementRepository(connection).latest(context_id)
    assert acknowledgement is not None
    services.writing.confirm_context_by_id(
        project_id, context_id, acknowledgement.raw_version
    )
    preparation = services.writing.prepare_unit_for_context(
        project_id, context_id, "START"
    )
    raw_v1 = (
        "Versão inicial preservada com conteúdo suficiente para validação.\n\n"
        "A análise inicial mantém a sequência histórica da unidade."
    ).encode()
    first_submission = services.writing.import_unit_by_preparation_id(
        project_id, preparation.id, raw_v1
    )
    assert first_submission.raw_version == 1
    prepared = browser.prepare_unit(project_id, "START", context_id=context_id)
    assert prepared.preparation_id == preparation.id
    with (
        services.database.connection() as connection,
        services.database.transaction(connection),
    ):
        sending = browser.persistence.begin_send(
            connection,
            prepared,
            pre_send_turn_anchor=_persisted_unit_turn_anchor(),
        )
    browser.block_orphaned_sending_interaction(project_id, sending.id)
    contaminated = (
        "Editar\nA resposta limpa desenvolve conteúdo suficiente para validação.\n\n"
        "A análise permanece íntegra e vinculada à preparação persistida."
    )
    clean = contaminated.removeprefix("Editar\n")
    adapter.current_path = DEFAULT_CONVERSATION_PATH
    adapter.conversations[DEFAULT_CONVERSATION_PATH][sending.transport_fingerprint] = (
        contaminated
    )
    if source_status == "captured":
        with pytest.raises(RuntimeError, match="browser.after_m2_import"):
            _service(services, adapter, "browser.after_m2_import").reconcile_blocked_interaction(
                project_id, sending.id
            )
        browser = _service(services, adapter)
    else:
        browser.reconcile_blocked_interaction(project_id, sending.id)
    with (
        services.database.connection() as connection,
        services.database.transaction(connection),
    ):
        # Recapture is intentionally legacy/ordinal; materialize that historical proof exactly.
        reconciliation_events: list[dict[str, object]] = []
        for event in BrowserInteractionRepository(connection).events(sending.id):
            event_evidence = event.get("evidence")
            if (
                isinstance(event_evidence, dict)
                and event_evidence.get("event_type") == "explicit_reconciliation"
            ):
                reconciliation_events.append(event)
        assert len(reconciliation_events) == 1
        legacy_proof = reconciliation_events[0]
        legacy_evidence = legacy_proof["evidence"]
        assert isinstance(legacy_evidence, dict)
        assert legacy_evidence["proof_kind"] == "persisted_unit_local_successor_v1"
        legacy_evidence["proof_kind"] = "persisted_unit_ordinal_v1"
        connection.execute(
            "UPDATE browser_interaction_events SET evidence_json = ? WHERE id = ?",
            (
                json.dumps(
                    legacy_evidence,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                legacy_proof["id"],
            ),
        )
    with services.database.connection() as connection:
        before = BrowserInteractionRepository(connection).get(sending.id)
        contaminated_submission = SubmissionRepository(connection).by_preparation_hash(
            preparation.id, sha256_bytes(contaminated.encode())
        )
        response_artifact_count_before = len(
            [
                artifact
                for artifact in ArtifactRepository(connection).list_for_project(project_id)
                if artifact.artifact_type == "browser_response_raw"
                and artifact.stage_run_id == before.stage_run_id
            ]
        )
        preparation_count_before = connection.execute(
            "SELECT count(*) FROM writing_unit_preparations WHERE project_id = ?",
            (project_id,),
        ).fetchone()[0]
        project = ProjectRepository(connection).get(project_id)
    assert before.status.value == source_status
    assert contaminated_submission is not None
    assert contaminated_submission.raw_version == 2
    assert before.response_artifact_id is not None
    old_response_artifact_id = before.response_artifact_id
    old_response_sha = before.response_sha256
    raw_v2_path = services.store.resolve(
        project.artifact_root, "text/raw/START/v0002.txt"
    )
    raw_v2_before = raw_v2_path.read_bytes()
    sent_before = tuple(adapter.sent_messages)
    adapter.conversations[DEFAULT_CONVERSATION_PATH][sending.transport_fingerprint] = clean

    result = browser.recapture_unit_response(project_id, sending.id)

    assert result["outcome"] == "recaptured"
    assert result["raw_version"] == 3
    assert result["response_artifact_created"] is True
    assert result["user_turn_ordinal"] == 1
    assert adapter.recaptured_ordinals == [1]
    assert adapter.sent_messages == list(sent_before)
    with services.database.connection() as connection:
        recaptured = BrowserInteractionRepository(connection).get(sending.id)
        clean_submission = SubmissionRepository(connection).get(
            str(recaptured.imported_entity_id)
        )
        artifacts = ArtifactRepository(connection).list_for_project(project_id)
        response_artifacts = [
            artifact
            for artifact in artifacts
            if artifact.artifact_type == "browser_response_raw"
            and artifact.stage_run_id == recaptured.stage_run_id
        ]
        events = BrowserInteractionRepository(connection).events(sending.id)
    assert recaptured.status is InteractionStatus.IMPORTED
    assert recaptured.response_artifact_id != old_response_artifact_id
    assert recaptured.response_sha256 != old_response_sha
    assert recaptured.imported_entity_id == clean_submission.id
    assert clean_submission.preparation_id == preparation.id
    assert clean_submission.raw_version == 3
    assert clean_submission.raw_sha256 == sha256_bytes(clean.encode())
    assert len(response_artifacts) == response_artifact_count_before + 1
    assert any(artifact.id == old_response_artifact_id for artifact in artifacts)
    assert raw_v2_path.read_bytes() == raw_v2_before == contaminated.encode()
    assert services.store.resolve(
        project.artifact_root, "text/raw/START/v0003.txt"
    ).read_bytes() == clean.encode()
    recapture_evidence = events[-1]["evidence"]
    assert isinstance(recapture_evidence, dict)
    assert recapture_evidence["event_type"] == "response_recaptured"
    assert recapture_evidence["previous_response_artifact_id"] == (
        old_response_artifact_id
    )

    repeated = browser.recapture_unit_response(project_id, sending.id)

    assert repeated["outcome"] == "already_current"
    assert repeated["raw_version"] == 3
    assert repeated["response_artifact_created"] is False
    assert adapter.recaptured_ordinals == [1, 1]
    assert adapter.sent_messages == list(sent_before)
    with services.database.connection() as connection:
        assert len(
            [
                artifact
                for artifact in ArtifactRepository(connection).list_for_project(project_id)
                if artifact.artifact_type == "browser_response_raw"
                and artifact.stage_run_id == recaptured.stage_run_id
            ]
        ) == len(response_artifacts)
        assert len(
            SubmissionRepository(connection).list_for_context(context_id)
        ) == 3
        assert connection.execute(
            "SELECT count(*) FROM writing_unit_preparations WHERE project_id = ?",
            (project_id,),
        ).fetchone()[0] == preparation_count_before


def test_observational_recapture_fails_before_browser_on_preparation_drift(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project_id, context_id = _automation_context(
        services, tmp_path, project_config_path, repository_root
    )
    adapter = FakeChatAdapter()
    browser = _service(services, adapter)
    browser.run_context(project_id, context_id)
    with services.database.connection() as connection:
        acknowledgement = AcknowledgementRepository(connection).latest(context_id)
    assert acknowledgement is not None
    services.writing.confirm_context_by_id(
        project_id, context_id, acknowledgement.raw_version
    )
    prepared = browser.prepare_unit(project_id, "START", context_id=context_id)
    with (
        services.database.connection() as connection,
        services.database.transaction(connection),
    ):
        sending = browser.persistence.begin_send(
            connection,
            prepared,
            pre_send_turn_anchor=_persisted_unit_turn_anchor(),
        )
    browser.block_orphaned_sending_interaction(project_id, sending.id)
    adapter.current_path = DEFAULT_CONVERSATION_PATH
    adapter.conversations[DEFAULT_CONVERSATION_PATH][sending.transport_fingerprint] = (
        "Resposta persistida com extensão adequada para validação determinística."
    )
    browser.reconcile_blocked_interaction(project_id, sending.id)
    replacement = services.writing.prepare_unit_for_context(
        project_id, context_id, "START", reprocess=True
    )
    with (
        services.database.connection() as connection,
        services.database.transaction(connection),
    ):
        connection.execute(
            "UPDATE browser_interactions SET preparation_id = ? WHERE id = ?",
            (replacement.id, sending.id),
        )
    sent_before = tuple(adapter.sent_messages)

    with pytest.raises(IntegrityError) as captured:
        browser.recapture_unit_response(project_id, sending.id)

    assert captured.value.code == "BROWSER_RECAPTURE_PREPARATION_MISMATCH"
    assert adapter.recaptured_ordinals == []
    assert adapter.sent_messages == list(sent_before)


def test_explicit_unit_reconcile_fails_closed_on_preparation_binding_mismatch(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project_id, context_id = _automation_context(
        services, tmp_path, project_config_path, repository_root
    )
    adapter = FakeChatAdapter()
    browser = _service(services, adapter)
    browser.run_context(project_id, context_id)
    with services.database.connection() as connection:
        acknowledgement = AcknowledgementRepository(connection).latest(context_id)
    assert acknowledgement is not None
    services.writing.confirm_context_by_id(
        project_id, context_id, acknowledgement.raw_version
    )
    prepared = browser.prepare_unit(project_id, "START", context_id=context_id)
    with (
        services.database.connection() as connection,
        services.database.transaction(connection),
    ):
        sending = browser.persistence.begin_send(
            connection,
            prepared,
            pre_send_turn_anchor=_persisted_unit_turn_anchor(),
        )
    browser.block_orphaned_sending_interaction(project_id, sending.id)
    other_preparation = services.writing.prepare_unit_for_context(
        project_id, context_id, "START", reprocess=True
    )
    with (
        services.database.connection() as connection,
        services.database.transaction(connection),
    ):
        connection.execute(
            "UPDATE browser_interactions SET preparation_id = ? WHERE id = ?",
            (other_preparation.id, sending.id),
        )
    sent_count_before = len(adapter.sent_messages)

    with pytest.raises(ConflictError) as captured:
        browser.reconcile_blocked_interaction(project_id, sending.id)

    assert captured.value.code == "BROWSER_RECONCILE_UNIT_PREPARATION_MISMATCH"
    assert len(adapter.sent_messages) == sent_count_before
    with services.database.connection() as connection:
        unchanged = BrowserInteractionRepository(connection).get(sending.id)
    assert unchanged.status is InteractionStatus.BLOCKED
    assert unchanged.response_artifact_id is None


@pytest.mark.parametrize(
    ("divergence", "expected_code"),
    [
        ("missing_boundary", "BROWSER_ORPHANED_SEND_BOUNDARY_MISSING"),
        ("sent_at", "BROWSER_ORPHANED_SEND_DOWNSTREAM_EVIDENCE"),
        ("stage_run", "BROWSER_ORPHANED_SEND_STAGE_RUN_INVALID"),
    ],
)
def test_explicit_orphaned_send_block_fails_closed_on_divergent_evidence(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
    divergence: str,
    expected_code: str,
) -> None:
    project_id, context_id = _automation_context(
        services, tmp_path, project_config_path, repository_root
    )
    adapter = FakeChatAdapter()
    browser = _service(services, adapter)
    prepared = browser.prepare_context(project_id, context_id)
    with (
        services.database.connection() as connection,
        services.database.transaction(connection),
    ):
        sending = browser.persistence.begin_send(connection, prepared)
        if divergence == "missing_boundary":
            connection.execute(
                "DELETE FROM browser_interaction_events "
                "WHERE interaction_id = ? AND status = 'sending'",
                (sending.id,),
            )
        elif divergence == "sent_at":
            BrowserInteractionRepository(connection).update(
                replace(sending, sent_at=utc_now()),
                expected_status=InteractionStatus.SENDING,
            )
        else:
            connection.execute(
                "UPDATE stage_runs SET status = 'failed', finished_at = ? WHERE id = ?",
                (utc_now(), sending.stage_run_id),
            )

    with pytest.raises(ConflictError) as captured:
        browser.block_orphaned_sending_interaction(project_id, sending.id)

    assert captured.value.code == expected_code
    assert adapter.sent_messages == []
    with services.database.connection() as connection:
        unchanged = BrowserInteractionRepository(connection).get(sending.id)
        events = BrowserInteractionRepository(connection).events(sending.id)
    assert unchanged.status is InteractionStatus.SENDING
    assert all(event["status"] != "blocked" for event in events)


def test_explicit_reconcile_falls_back_to_recent_fingerprint_proven_candidate(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project_id, context_id, adapter, browser = _blocked_unprovable_context(
        services, tmp_path, project_config_path, repository_root
    )
    adapter.current_path = OLD_CONVERSATION_PATH
    with services.database.connection() as connection:
        before = BrowserInteractionRepository(connection).latest_for_context_kind(
            context_id, InteractionKind.CONTEXT_LOAD
        )
        assert before is not None
    send_count_before = len(adapter.sent_messages)

    result = browser.reconcile_blocked_interaction(project_id, before.id)

    assert result["outcome"] == "reconciled"
    assert result["conversation_path"] == DEFAULT_CONVERSATION_PATH
    assert adapter.current_path == DEFAULT_CONVERSATION_PATH
    assert len(adapter.sent_messages) == send_count_before
    with services.database.connection() as connection:
        after = BrowserInteractionRepository(connection).get(before.id)
    assert after.status is InteractionStatus.IMPORTED
    assert after.attempt == before.attempt


@pytest.mark.parametrize("failure", ["zero", "multiple", "fingerprint", "incomplete"])
def test_explicit_reconcile_failures_leave_interaction_blocked_without_resend(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
    failure: str,
) -> None:
    project_id, context_id, adapter, browser = _blocked_unprovable_context(
        services, tmp_path, project_config_path, repository_root
    )
    with services.database.connection() as connection:
        before = BrowserInteractionRepository(connection).latest_for_context_kind(
            context_id, InteractionKind.CONTEXT_LOAD
        )
        assert before is not None
        events_before = BrowserInteractionRepository(connection).events(before.id)
    send_count_before = len(adapter.sent_messages)
    if failure == "zero":
        adapter.conversations.pop(DEFAULT_CONVERSATION_PATH)
    elif failure == "multiple":
        second_path = "/c/00000000-0000-4000-8000-000000000002"
        adapter.current_path = OLD_CONVERSATION_PATH
        adapter.conversations[second_path] = {
            before.transport_fingerprint: adapter.next_response
        }
    elif failure == "fingerprint":
        candidate_path = "/c/00000000-0000-4000-8000-000000000003"
        adapter.current_path = OLD_CONVERSATION_PATH
        adapter.bootstrap_override = (
            BootstrapCandidate(candidate_path, before.transport_fingerprint, utc_now()),
        )
        adapter.conversations[candidate_path] = {
            transport_fingerprint("different request"): adapter.next_response
        }
    else:
        adapter.inspect_override = TurnState.STREAMING

    with pytest.raises(ConflictError) as captured:
        browser.reconcile_blocked_interaction(project_id, before.id)

    expected_codes = {
        "zero": "BROWSER_RECONCILE_CANDIDATE_NOT_FOUND",
        "multiple": "BROWSER_RECONCILE_CANDIDATE_AMBIGUOUS",
        "fingerprint": "BROWSER_RECONCILE_FINGERPRINT_MISMATCH",
        "incomplete": "BROWSER_RECONCILE_RESPONSE_INCOMPLETE",
    }
    assert captured.value.code == expected_codes[failure]
    assert len(adapter.sent_messages) == send_count_before
    with services.database.connection() as connection:
        after = BrowserInteractionRepository(connection).get(before.id)
        conversation = BrowserConversationRepository(connection).get(after.conversation_id)
        events_after = BrowserInteractionRepository(connection).events(before.id)
    assert after.status is InteractionStatus.BLOCKED
    assert after.attempt == before.attempt
    assert after.response_artifact_id is None
    assert conversation.conversation_path is None
    assert events_after == events_before


@pytest.mark.parametrize("source", ["not_blocked", "wrong_error", "abandoned"])
def test_explicit_reconcile_requires_unresolved_send_not_provable_source(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> None:
    if source == "abandoned":
        project_id, context_id, adapter, browser = _blocked_unprovable_context(
            services, tmp_path, project_config_path, repository_root
        )
        with services.database.connection() as connection:
            item = BrowserInteractionRepository(connection).latest_for_context_kind(
                context_id, InteractionKind.CONTEXT_LOAD
            )
            assert item is not None
        browser.abandon_interaction(
            project_id,
            item.id,
            operator="operator",
            reason="explicit test resolution",
        )
        expected_code = "BROWSER_RECONCILE_INTERACTION_ABANDONED"
    else:
        project_id, context_id = _automation_context(
            services, tmp_path, project_config_path, repository_root
        )
        adapter = FakeChatAdapter()
        browser = _service(services, adapter)
        item = browser.prepare_context(project_id, context_id)
        expected_code = "BROWSER_RECONCILE_SOURCE_INVALID"
        if source == "wrong_error":
            with pytest.raises(IntegrityError):
                browser._block(  # noqa: SLF001 - focused eligibility fixture.
                    item,
                    "BROWSER_OTHER_BLOCK",
                    "Synthetic non-send-proof block",
                )
            with services.database.connection() as connection:
                item = BrowserInteractionRepository(connection).get(item.id)
    send_count_before = len(adapter.sent_messages)
    monkeypatch.setattr(
        adapter,
        "ensure_ready",
        lambda: (_ for _ in ()).throw(
            AssertionError("ineligible reconciliation must not open the browser")
        ),
    )

    with pytest.raises(ConflictError) as captured:
        browser.reconcile_blocked_interaction(project_id, item.id)

    assert captured.value.code == expected_code
    assert len(adapter.sent_messages) == send_count_before
    with services.database.connection() as connection:
        interactions = BrowserInteractionRepository(connection).list_for_project(project_id)
    assert len(interactions) == 1


def test_recovery_skips_prepared_interaction_without_composer_or_send(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id, context_id = _automation_context(
        services, tmp_path, project_config_path, repository_root
    )
    adapter = FakeChatAdapter()
    browser = _service(services, adapter)
    prepared = browser.prepare_context(project_id, context_id)

    def forbidden_send(_text: str) -> str | None:
        raise AssertionError("recovery must not call send_message")

    monkeypatch.setattr(adapter, "send_message", forbidden_send)
    report = BrowserRecovery(browser).recover(project_id)

    assert _recovered_items(report) == []
    assert _skipped_items(report) == [
        {
            "interaction_id": prepared.id,
            "status": "prepared",
            "kind": "context_load",
            "unit_id": None,
            "reason": "not_started_requires_explicit_run",
            "requires_action": True,
        }
    ]
    assert adapter.sent_messages == []
    with services.database.connection() as connection:
        assert (
            BrowserInteractionRepository(connection).get(prepared.id).status
            is InteractionStatus.PREPARED
        )


def test_operator_abandons_unprovable_context_attempt_and_explicit_run_supersedes_it(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id, context_id = _automation_context(
        services, tmp_path, project_config_path, repository_root
    )
    adapter = FakeChatAdapter()
    browser = _service(services, adapter)

    def interrupt_send(
        _text: str,
        *,
        on_send_attempt_started: Callable[[], None] | None = None,
    ) -> str | None:
        assert on_send_attempt_started is not None
        on_send_attempt_started()
        raise KeyboardInterrupt

    monkeypatch.setattr(adapter, "send_message", interrupt_send)
    with pytest.raises(KeyboardInterrupt):
        browser.run_context(project_id, context_id)

    with services.database.connection() as connection:
        original = BrowserInteractionRepository(connection).latest_for_context_kind(
            context_id, InteractionKind.CONTEXT_LOAD
        )
        assert original is not None and original.status is InteractionStatus.SENDING
        original_run = connection.execute(
            "SELECT status FROM stage_runs WHERE id = ?", (original.stage_run_id,)
        ).fetchone()
        original_artifact = connection.execute(
            "SELECT id, sha256 FROM artifacts WHERE id = ?", (original.request_artifact_id,)
        ).fetchone()
    assert original_run is not None and original_run["status"] == "running"
    assert original_artifact is not None

    adapter.bootstrap_override = ()
    recovery = BrowserRecovery(_service(services, adapter)).recover(project_id)
    assert _skipped_items(recovery)[0]["error_code"] == "BROWSER_BOOTSTRAP_AMBIGUOUS"

    restarted = _service(services, adapter)
    with pytest.raises(ConflictError) as blocked_run:
        restarted.run_context(project_id, context_id)
    assert blocked_run.value.code == "BROWSER_INTERACTION_BLOCKED"
    with pytest.raises(ConflictError) as blocked_continue:
        restarted.continue_project(project_id)
    assert blocked_continue.value.code == "BROWSER_INTERACTION_BLOCKED"
    status = restarted.status(project_id)
    assert status["blocked_interactions"] == [
        {
            "interaction_id": original.id,
            "context_id": context_id,
            "kind": "context_load",
            "unit_id": None,
            "operator_abandoned": False,
        }
    ]
    shown = restarted.interaction_show(project_id)
    assert shown["interaction"]["id"] == original.id  # type: ignore[index]
    assert shown["operator_resolution"] is None

    resolution = restarted.abandon_interaction(
        project_id,
        original.id,
        operator="operator-test",
        reason="Ctrl+C durante o controle de envio; efeito externo não comprovável.",
    )
    assert resolution["resolution"] == "operator_abandoned"
    assert resolution["conversation_action"] == "provisioning_attempt_closed"
    assert resolution["operator"] == "operator-test"
    resolution_reason = resolution["reason"]
    assert isinstance(resolution_reason, str) and resolution_reason.startswith("Ctrl+C")
    assert isinstance(resolution["created_at"], str)
    assert "not sent" not in str(resolution).lower()
    assert (
        restarted.abandon_interaction(
            project_id,
            original.id,
            operator="operator-test",
            reason="Ctrl+C durante o controle de envio; efeito externo não comprovável.",
        )
        == resolution
    )
    assert restarted.status(project_id)["blocked_interactions"] == [
        {
            "interaction_id": original.id,
            "context_id": context_id,
            "kind": "context_load",
            "unit_id": None,
            "operator_abandoned": True,
        }
    ]

    after_restart = _service(services, adapter)
    audit = after_restart.interaction_show(project_id, original.id)
    assert audit["operator_resolution"] == resolution
    skipped_report = BrowserRecovery(after_restart).recover(project_id)
    assert _recovered_items(skipped_report) == []
    assert _skipped_items(skipped_report)[0]["reason"] == "operator_abandoned"
    with services.database.connection() as connection:
        preserved = BrowserInteractionRepository(connection).get(original.id)
        assert preserved.status is InteractionStatus.BLOCKED
        run = connection.execute(
            "SELECT status FROM stage_runs WHERE id = ?", (original.stage_run_id,)
        ).fetchone()
        artifact = connection.execute(
            "SELECT id, sha256 FROM artifacts WHERE id = ?", (original.request_artifact_id,)
        ).fetchone()
        event_count = connection.execute(
            "SELECT count(*) FROM browser_interaction_events WHERE interaction_id = ?",
            (original.id,),
        ).fetchone()[0]
    assert run is not None and run["status"] == "blocked"
    assert artifact is not None
    assert tuple(artifact) == tuple(original_artifact)
    assert event_count >= 4

    monkeypatch.setattr(
        adapter,
        "send_message",
        FakeChatAdapter.send_message.__get__(adapter, FakeChatAdapter),
    )
    adapter.bootstrap_override = None
    result = after_restart.run_context(project_id, context_id)
    assert result["status"] == "imported"
    with services.database.connection() as connection:
        successor = BrowserInteractionRepository(connection).latest_for_context_kind(
            context_id, InteractionKind.CONTEXT_LOAD
        )
        assert successor is not None
        successor_run = connection.execute(
            "SELECT supersedes_run_id FROM stage_runs WHERE id = ?",
            (successor.stage_run_id,),
        ).fetchone()
        assert connection.execute(
            "SELECT count(*) FROM browser_interactions WHERE context_id = ?", (context_id,)
        ).fetchone()[0] == 2
    assert successor.id != original.id
    assert successor.supersedes_interaction_id == original.id
    assert successor_run is not None and successor_run[0] == original.stage_run_id


def _exhausted_blocked_context(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
) -> tuple[str, str, FakeChatAdapter, BrowserAutomationService]:
    project_id, context_id = _automation_context(
        services, tmp_path, project_config_path, repository_root
    )
    adapter = FakeChatAdapter()
    browser = _service(services, adapter)
    prepared = browser.prepare_context(project_id, context_id)
    with (
        services.database.connection() as connection,
        services.database.transaction(connection),
    ):
        connection.execute(
            "UPDATE browser_interactions SET attempt = max_attempts WHERE id = ?",
            (prepared.id,),
        )
    with pytest.raises(IntegrityError) as blocked:
        browser._block(  # noqa: SLF001 - focused terminal-lineage fixture.
            prepared,
            "BROWSER_SEND_NOT_PROVABLE",
            "Synthetic exhausted interaction",
        )
    assert blocked.value.code == "BROWSER_SEND_NOT_PROVABLE"
    return project_id, context_id, adapter, browser


def test_exhausted_unresolved_context_lineage_stays_blocked_without_browser(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id, context_id, adapter, browser = _exhausted_blocked_context(
        services, tmp_path, project_config_path, repository_root
    )
    monkeypatch.setattr(
        adapter,
        "ensure_ready",
        lambda: (_ for _ in ()).throw(AssertionError("browser must not be opened")),
    )

    with pytest.raises(ConflictError) as captured:
        browser.run_context(project_id, context_id)

    assert captured.value.code in {
        "BROWSER_ATTEMPTS_EXHAUSTED",
        "BROWSER_INTERACTION_BLOCKED",
    }
    with services.database.connection() as connection:
        interactions = BrowserInteractionRepository(connection).list_for_project(project_id)
    assert len(interactions) == 1
    assert interactions[0].status is InteractionStatus.BLOCKED
    assert interactions[0].attempt == interactions[0].max_attempts


def test_abandoned_exhausted_context_starts_fresh_immutable_lineage_without_browser(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id, context_id, adapter, browser = _exhausted_blocked_context(
        services, tmp_path, project_config_path, repository_root
    )
    with services.database.connection() as connection:
        original = BrowserInteractionRepository(connection).latest_for_context_kind(
            context_id, InteractionKind.CONTEXT_LOAD
        )
        assert original is not None
    browser.abandon_interaction(
        project_id,
        original.id,
        operator="operator-test",
        reason="Terminal exhausted interaction explicitly abandoned",
    )
    with services.database.connection() as connection:
        interactions = BrowserInteractionRepository(connection)
        original = interactions.get(original.id)
        original_events = interactions.events(original.id)
        original_resolution = BrowserInteractionResolutionRepository(
            connection
        ).for_interaction(original.id)
        original_conversation = BrowserConversationRepository(connection).get(
            original.conversation_id
        )
        original_run = dict(
            connection.execute(
                "SELECT * FROM stage_runs WHERE id = ?", (original.stage_run_id,)
            ).fetchone()
        )
        original_artifacts = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM artifacts WHERE stage_run_id = ? ORDER BY id",
                (original.stage_run_id,),
            ).fetchall()
        ]
    monkeypatch.setattr(
        adapter,
        "ensure_ready",
        lambda: (_ for _ in ()).throw(AssertionError("browser must not be opened")),
    )

    successor = browser.prepare_context(project_id, context_id)

    assert successor.status is InteractionStatus.PREPARED
    assert successor.attempt == 1
    assert successor.max_attempts == browser.config.max_attempts
    assert successor.supersedes_interaction_id == original.id
    assert successor.conversation_id != original.conversation_id
    with services.database.connection() as connection:
        interactions = BrowserInteractionRepository(connection)
        successor_run = dict(
            connection.execute(
                "SELECT * FROM stage_runs WHERE id = ?", (successor.stage_run_id,)
            ).fetchone()
        )
        assert interactions.get(original.id) == original
        assert interactions.events(original.id) == original_events
        assert (
            BrowserInteractionResolutionRepository(connection).for_interaction(original.id)
            == original_resolution
        )
        assert (
            BrowserConversationRepository(connection).get(original.conversation_id)
            == original_conversation
        )
        assert dict(
            connection.execute(
                "SELECT * FROM stage_runs WHERE id = ?", (original.stage_run_id,)
            ).fetchone()
        ) == original_run
        assert [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM artifacts WHERE stage_run_id = ? ORDER BY id",
                (original.stage_run_id,),
            ).fetchall()
        ] == original_artifacts
        assert len(interactions.list_for_project(project_id)) == 2
    assert successor_run["status"] == "pending"
    assert successor_run["supersedes_run_id"] == original.stage_run_id


def test_operator_abandon_rejects_nonblocked_interaction(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project_id, context_id = _automation_context(
        services, tmp_path, project_config_path, repository_root
    )
    browser = _service(services, FakeChatAdapter())
    prepared = browser.prepare_context(project_id, context_id)
    with pytest.raises(ConflictError) as captured:
        browser.abandon_interaction(
            project_id, prepared.id, operator="operator-test", reason="invalid state"
        )
    assert captured.value.code == "BROWSER_ABANDON_STATE_INVALID"


def test_interaction_show_prefers_active_and_requires_id_when_active_is_ambiguous(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project_id, context_id = _automation_context(
        services, tmp_path, project_config_path, repository_root
    )
    browser = _service(services, FakeChatAdapter())
    historical = browser.prepare_context(project_id, context_id)
    with (
        services.database.connection() as connection,
        services.database.transaction(connection),
    ):
        historical = browser.persistence.begin_send(connection, historical)
    with pytest.raises(IntegrityError):
        browser._block(  # noqa: SLF001 - synthetic unresolved external attempt.
            historical,
            "BROWSER_SEND_NOT_PROVABLE",
            "Synthetic historical ambiguity",
        )
    browser.abandon_interaction(
        project_id,
        historical.id,
        operator="operator-test",
        reason="Synthetic historical attempt abandoned",
    )

    active = browser.prepare_context(project_id, context_id)
    with (
        services.database.connection() as connection,
        services.database.transaction(connection),
    ):
        active = browser.persistence.begin_send(connection, active)
    status = browser.status(project_id)
    active_payload = status["active_interactions"]
    assert isinstance(active_payload, list)
    assert [item["interaction_id"] for item in active_payload] == [active.id]
    assert browser.interaction_show(project_id)["interaction"]["id"] == active.id  # type: ignore[index]
    assert (
        browser.interaction_show(project_id, historical.id)["interaction"]["id"]  # type: ignore[index]
        == historical.id
    )

    with (
        services.database.connection() as connection,
        services.database.transaction(connection),
    ):
        extra_conversation = browser.persistence._new_conversation(  # noqa: SLF001
            project_id, context_id
        )
        BrowserConversationRepository(connection).add(extra_conversation)
        second_active = browser.persistence.create_interaction(
            connection,
            project_id=project_id,
            conversation_id=extra_conversation.id,
            context_id=context_id,
            kind=InteractionKind.CONTEXT_LOAD,
            request_artifact_id=active.request_artifact_id,
            request_sha256=active.request_sha256,
            transport_fingerprint=active.transport_fingerprint,
        )
    with pytest.raises(ConflictError) as ambiguous:
        browser.interaction_show(project_id)
    assert ambiguous.value.code == "BROWSER_INTERACTION_SELECTION_AMBIGUOUS"
    evidence = ambiguous.value.context.evidence
    assert evidence is not None
    candidates = evidence["candidates"]
    assert isinstance(candidates, list)
    assert {item["interaction_id"] for item in candidates} == {
        active.id,
        second_active.id,
    }


def test_crash_during_streaming_recovers_known_conversation_without_resend(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project_id, context_id = _automation_context(
        services, tmp_path, project_config_path, repository_root
    )
    adapter = FakeChatAdapter()
    adapter.inspect_override = TurnState.STREAMING
    with pytest.raises(RuntimeError, match="browser.streaming"):
        _service(services, adapter, "browser.streaming").run_context(project_id, context_id)
    with services.database.connection() as connection:
        row = connection.execute(
            "SELECT i.status, c.conversation_path FROM browser_interactions i "
            "JOIN browser_conversations c ON c.id = i.conversation_id "
            "WHERE i.context_id = ?",
            (context_id,),
        ).fetchone()
    assert row is not None
    assert row["status"] == "streaming"
    assert row["conversation_path"] == DEFAULT_CONVERSATION_PATH
    adapter.inspect_override = None
    recovered = BrowserRecovery(_service(services, adapter)).recover(project_id)
    assert _recovered_items(recovered)[0]["status"] == "imported"
    assert len(adapter.sent_messages) == 1


def test_sent_unit_local_proof_recovery_captures_and_imports_without_legacy_proof_or_send(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project_id, interaction_id, adapter = _sent_local_unit_for_recovery(
        services,
        tmp_path,
        project_config_path,
        repository_root,
    )
    with services.database.connection() as connection:
        before = BrowserInteractionRepository(connection).get(interaction_id)
        before_events = BrowserInteractionRepository(connection).events(interaction_id)
    assert before.status is InteractionStatus.SENT
    assert before.response_artifact_id is None
    assert any(
        isinstance(evidence := event.get("evidence"), dict)
        and evidence.get("proof_kind") == "post_send_local_successor_v1"
        for event in before_events
    )
    sent_before = tuple(adapter.sent_messages)
    adapter.reject_legacy_inspection = True
    adapter.legacy_inspection_count = 0

    report = BrowserRecovery(_service(services, adapter)).recover(project_id)

    recovered = _recovered_items(report)
    assert len(recovered) == 1
    assert recovered[0]["status"] == "imported"
    assert adapter.legacy_inspection_count == 0
    assert adapter.sent_messages == list(sent_before)
    assert adapter.reconciliation_bindings
    assert all(isinstance(binding, dict) for binding in adapter.reconciliation_bindings)
    with services.database.connection() as connection:
        final = BrowserInteractionRepository(connection).get(interaction_id)
        run = StageRunRepository(connection).get(final.stage_run_id)
    assert final.status is InteractionStatus.IMPORTED
    assert final.response_artifact_id is not None
    assert final.response_sha256 is not None
    assert run.status is RunStatus.DONE
    assert run.finished_at is not None


def test_sent_unit_recovery_late_binds_placeholder_to_real_assistant(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    placeholder_id = "request-placeholder-unit-response"
    real_assistant_id = "d2df9573-2242-49a3-837f-9dd727b20c23"
    project_id, interaction_id, adapter = _sent_local_unit_for_recovery(
        services,
        tmp_path,
        project_config_path,
        repository_root,
        sent_assistant_id=placeholder_id,
        recovery_assistant_id=real_assistant_id,
    )
    with (
        services.database.connection() as connection,
        services.database.transaction(connection),
    ):
        events = BrowserInteractionRepository(connection).events(interaction_id)
        sent_event = next(event for event in events if event["status"] == "sent")
        normalized_evidence = sent_event["evidence"]
        assert isinstance(normalized_evidence, dict)
        assert normalized_evidence["selected_assistant_id"] is None
        assert normalized_evidence["assistant_identity_state"] == "unresolved"
        historical_evidence = {
            **normalized_evidence,
            "selected_assistant_id": placeholder_id,
        }
        connection.execute(
            "UPDATE browser_interaction_events SET evidence_json = ? WHERE id = ?",
            (
                json.dumps(historical_evidence, sort_keys=True, separators=(",", ":")),
                sent_event["id"],
            ),
        )
    sent_before = tuple(adapter.sent_messages)
    adapter.reject_legacy_inspection = True
    adapter.legacy_inspection_count = 0

    report = BrowserRecovery(_service(services, adapter)).recover(project_id)

    recovered = _recovered_items(report)
    assert len(recovered) == 1
    assert recovered[0]["status"] == "imported"
    assert adapter.legacy_inspection_count == 0
    assert adapter.sent_messages == list(sent_before)
    with services.database.connection() as connection:
        final = BrowserInteractionRepository(connection).get(interaction_id)
        final_events = BrowserInteractionRepository(connection).events(interaction_id)
    late_bound = [
        event
        for event in final_events
        if isinstance(evidence := event.get("evidence"), dict)
        and evidence.get("event_type") == "assistant_successor_late_bound"
    ]
    assert final.status is InteractionStatus.IMPORTED
    assert len(late_bound) == 1
    late_bound_evidence = late_bound[0]["evidence"]
    assert isinstance(late_bound_evidence, dict)
    assert late_bound_evidence["selected_assistant_id"] == real_assistant_id
    assert late_bound_evidence["assistant_identity_state"] == "resolved"


def test_blocked_partial_local_binding_reconcile_late_binds_without_send(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project_id, _context_id, interaction_id, adapter, browser = (
        _blocked_partial_local_unit_for_recovery(
            services,
            tmp_path,
            project_config_path,
            repository_root,
        )
    )
    with services.database.connection() as connection:
        before = BrowserInteractionRepository(connection).get(interaction_id)
        before_events = BrowserInteractionRepository(connection).events(interaction_id)
    blocked_proof = next(
        evidence
        for event in before_events
        if event["status"] == InteractionStatus.BLOCKED.value
        and isinstance((evidence := event.get("evidence")), dict)
        and evidence.get("proof_kind") == "post_send_local_successor_v1"
    )
    assert before.status is InteractionStatus.BLOCKED
    assert before.sent_at is None
    assert before.response_artifact_id is None
    assert blocked_proof["selected_assistant_id"].startswith("request-placeholder-")
    assert not any(
        isinstance(evidence := event.get("evidence"), dict)
        and evidence.get("effect_boundary") == "send_observed"
        for event in before_events
    )
    sent_before = tuple(adapter.sent_messages)
    adapter.reject_legacy_inspection = True

    reconciled = browser.reconcile_blocked_interaction(project_id, interaction_id)

    result = reconciled["result"]
    assert isinstance(result, dict)
    assert reconciled["outcome"] == "reconciled"
    assert result["status"] == "imported"
    assert adapter.legacy_inspection_count == 0
    assert adapter.sent_messages == list(sent_before)
    with services.database.connection() as connection:
        final = BrowserInteractionRepository(connection).get(interaction_id)
        events = BrowserInteractionRepository(connection).events(interaction_id)
    assert final.status is InteractionStatus.IMPORTED
    assert final.response_artifact_id is not None
    assert final.imported_entity_id is not None
    assert any(
        isinstance(evidence := event.get("evidence"), dict)
        and evidence.get("event_type") == "assistant_successor_late_bound"
        and evidence.get("external_action_performed") is False
        for event in events
    )


def test_browser_recovery_accepts_blocked_partial_local_binding_without_send(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project_id, _context_id, interaction_id, adapter, _browser = (
        _blocked_partial_local_unit_for_recovery(
            services,
            tmp_path,
            project_config_path,
            repository_root,
        )
    )
    sent_before = tuple(adapter.sent_messages)
    adapter.reject_legacy_inspection = True

    report = BrowserRecovery(_service(services, adapter)).recover(project_id)

    recovered = _recovered_items(report)
    assert len(recovered) == 1
    assert recovered[0]["status"] == "imported"
    assert adapter.legacy_inspection_count == 0
    assert adapter.sent_messages == list(sent_before)
    with services.database.connection() as connection:
        final = BrowserInteractionRepository(connection).get(interaction_id)
    assert final.status is InteractionStatus.IMPORTED


def test_recovery_restart_reuses_persisted_assistant_latch_without_send(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project_id, _context_id, interaction_id, adapter, _browser = (
        _blocked_partial_local_unit_for_recovery(
            services,
            tmp_path,
            project_config_path,
            repository_root,
        )
    )
    sent_before = tuple(adapter.sent_messages)
    adapter.reject_legacy_inspection = True

    with pytest.raises(RuntimeError, match="browser.assistant_successor_latched"):
        _service(
            services,
            adapter,
            "browser.assistant_successor_latched",
        ).reconcile_blocked_interaction(project_id, interaction_id)

    with services.database.connection() as connection:
        interrupted = BrowserInteractionRepository(connection).get(interaction_id)
        interrupted_events = BrowserInteractionRepository(connection).events(
            interaction_id
        )
    persisted_latches = [
        evidence
        for event in interrupted_events
        if isinstance((evidence := event.get("evidence")), dict)
        and evidence.get("event_type") == "assistant_successor_late_bound"
        and evidence.get("identity_latched_before_capture") is True
    ]
    assert interrupted.status is InteractionStatus.BLOCKED
    assert interrupted.response_artifact_id is None
    assert len(persisted_latches) == 1
    assert persisted_latches[0]["selected_assistant_id"] == (
        "assistant-" + interrupted.transport_fingerprint
    )

    restarted_adapter = _LocalProofRecoveryAdapter()
    restarted_adapter.current_path = adapter.current_path
    restarted_adapter.conversations = {
        path: dict(turns) for path, turns in adapter.conversations.items()
    }
    restarted_adapter.sent_messages = list(adapter.sent_messages)
    restarted_adapter.reject_legacy_inspection = True
    reconciled = _service(services, restarted_adapter).reconcile_blocked_interaction(
        project_id, interaction_id
    )

    result = reconciled["result"]
    assert isinstance(result, dict)
    assert result["status"] == "imported"
    assert restarted_adapter.legacy_inspection_count == 0
    assert restarted_adapter.sent_messages == list(sent_before)
    with services.database.connection() as connection:
        final_events = BrowserInteractionRepository(connection).events(interaction_id)
    assert sum(
        1
        for event in final_events
        if isinstance((evidence := event.get("evidence")), dict)
        and evidence.get("event_type") == "assistant_successor_late_bound"
        and evidence.get("identity_latched_before_capture") is True
    ) == 1


def test_blocked_partial_local_binding_without_assistant_stays_fail_closed(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project_id, _context_id, interaction_id, adapter, browser = (
        _blocked_partial_local_unit_for_recovery(
            services,
            tmp_path,
            project_config_path,
            repository_root,
            recovery_assistant_id="request-placeholder-still-unresolved",
        )
    )
    sent_before = tuple(adapter.sent_messages)
    adapter.reject_legacy_inspection = True

    with pytest.raises(ConflictError) as captured:
        browser.reconcile_blocked_interaction(project_id, interaction_id)

    assert captured.value.code == "BROWSER_RECOVERY_UNIT_LOCAL_BINDING_INVALID"
    assert adapter.legacy_inspection_count == 0
    assert adapter.sent_messages == list(sent_before)
    with services.database.connection() as connection:
        final = BrowserInteractionRepository(connection).get(interaction_id)
        events = BrowserInteractionRepository(connection).events(interaction_id)
    assert final.status is InteractionStatus.BLOCKED
    assert final.response_artifact_id is None
    assert final.imported_entity_id is None
    assert not any(
        isinstance(evidence := event.get("evidence"), dict)
        and evidence.get("event_type") == "assistant_successor_late_bound"
        for event in events
    )


def test_blocked_partial_local_binding_without_send_boundary_is_source_invalid(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id, _context_id, interaction_id, adapter, browser = (
        _blocked_partial_local_unit_for_recovery(
            services,
            tmp_path,
            project_config_path,
            repository_root,
        )
    )
    with (
        services.database.connection() as connection,
        services.database.transaction(connection),
    ):
        sending = next(
            event
            for event in BrowserInteractionRepository(connection).events(interaction_id)
            if event["status"] == InteractionStatus.SENDING.value
        )
        evidence = sending["evidence"]
        assert isinstance(evidence, dict)
        evidence.pop("effect_boundary")
        connection.execute(
            "UPDATE browser_interaction_events SET evidence_json = ? WHERE id = ?",
            (
                json.dumps(evidence, sort_keys=True, separators=(",", ":")),
                sending["id"],
            ),
        )
    sent_before = tuple(adapter.sent_messages)
    monkeypatch.setattr(
        adapter,
        "ensure_ready",
        lambda: (_ for _ in ()).throw(
            AssertionError("source-invalid reconcile must not open the browser")
        ),
    )

    with pytest.raises(ConflictError) as captured:
        browser.reconcile_blocked_interaction(project_id, interaction_id)

    assert captured.value.code == "BROWSER_RECONCILE_SOURCE_INVALID"
    assert adapter.sent_messages == list(sent_before)


def test_blocked_partial_local_binding_operator_abandoned_is_ineligible(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id, _context_id, interaction_id, adapter, browser = (
        _blocked_partial_local_unit_for_recovery(
            services,
            tmp_path,
            project_config_path,
            repository_root,
        )
    )
    browser.abandon_interaction(
        project_id,
        interaction_id,
        operator="operator",
        reason="focused local-binding abandonment",
    )
    sent_before = tuple(adapter.sent_messages)
    monkeypatch.setattr(
        adapter,
        "ensure_ready",
        lambda: (_ for _ in ()).throw(
            AssertionError("abandoned reconcile must not open the browser")
        ),
    )

    with pytest.raises(ConflictError) as captured:
        browser.reconcile_blocked_interaction(project_id, interaction_id)

    assert captured.value.code == "BROWSER_RECONCILE_INTERACTION_ABANDONED"
    assert adapter.sent_messages == list(sent_before)


def test_writing_runner_recovers_partial_local_block_after_accepted_unit_without_resend(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    adapter = _LocalProofRecoveryAdapter()
    project_id, _context_id, browser = _confirmed_browser_context(
        services,
        tmp_path,
        project_config_path,
        repository_root,
        adapter,
    )
    adapter.next_response = (
        "Texto acadêmico com extensão objetiva suficiente para aceitação direta."
    )
    adapter.partial_local_ambiguity_on_structural_call = (
        adapter.structural_inspection_count + 2
    )
    adapter.reject_legacy_inspection = True

    result = browser.run_writing(project_id)

    assert result["stop_reason"] == "complete"
    assert result["processed_units"] == ["START", "END"]
    assert result["accepted_units"] == ["START", "END"]
    recovered = result["recovered_interactions"]
    assert isinstance(recovered, list) and len(recovered) == 1
    assert adapter.legacy_inspection_count == 0
    assert len(adapter.sent_messages) == 3
    with services.database.connection() as connection:
        unit_interactions = [
            item
            for item in BrowserInteractionRepository(connection).list_for_project(
                project_id
            )
            if item.kind is InteractionKind.UNIT_REQUEST
        ]
    assert len(unit_interactions) == 2
    assert all(item.status is InteractionStatus.IMPORTED for item in unit_interactions)


def test_blocked_local_proof_reconcile_resumes_capture_after_recovery_block(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    placeholder_id = "request-placeholder-unit-response"
    project_id, interaction_id, adapter = _sent_local_unit_for_recovery(
        services,
        tmp_path,
        project_config_path,
        repository_root,
        sent_assistant_id=placeholder_id,
        recovery_user_id="wrong-local-user",
    )
    sent_before = tuple(adapter.sent_messages)
    adapter.reject_legacy_inspection = True
    adapter.legacy_inspection_count = 0

    first_report = BrowserRecovery(_service(services, adapter)).recover(project_id)

    skipped = _skipped_items(first_report)
    assert len(skipped) == 1
    assert skipped[0]["error_code"] == "BROWSER_RECOVERY_UNIT_LOCAL_BINDING_INVALID"
    with services.database.connection() as connection:
        blocked = BrowserInteractionRepository(connection).get(interaction_id)
        blocked_run = StageRunRepository(connection).get(blocked.stage_run_id)
    assert blocked.status is InteractionStatus.BLOCKED
    assert blocked.response_artifact_id is None
    assert blocked_run.status is RunStatus.BLOCKED
    assert blocked_run.finished_at is not None

    adapter.recovery_user_id = None
    reconciled = _service(services, adapter).reconcile_blocked_interaction(
        project_id, interaction_id
    )

    result = reconciled["result"]
    assert isinstance(result, dict)
    assert reconciled["outcome"] == "reconciled"
    assert reconciled["proof_kind"] == "post_send_local_successor_v1"
    assert result["status"] == "imported"
    assert adapter.legacy_inspection_count == 0
    assert adapter.sent_messages == list(sent_before)
    with services.database.connection() as connection:
        final = BrowserInteractionRepository(connection).get(interaction_id)
        run = StageRunRepository(connection).get(final.stage_run_id)
        events = BrowserInteractionRepository(connection).events(interaction_id)
    assert final.status is InteractionStatus.IMPORTED
    assert final.attempt == blocked.attempt
    assert final.response_artifact_id is not None
    assert final.response_sha256 is not None
    assert final.imported_entity_id is not None
    assert run.status is RunStatus.DONE
    assert run.finished_at is not None
    send_observed = [
        event
        for event in events
        if isinstance(evidence := event.get("evidence"), dict)
        and evidence.get("effect_boundary") == "send_observed"
        and evidence.get("proof_kind") == "post_send_local_successor_v1"
    ]
    assert len(send_observed) == 1
    assert any(
        isinstance(evidence := event.get("evidence"), dict)
        and evidence.get("event_type") == "assistant_successor_late_bound"
        and evidence.get("identity_latched_before_capture") is True
        and evidence.get("external_action_performed") is False
        for event in events
    )
    assert any(
        isinstance(evidence := event.get("evidence"), dict)
        and evidence.get("event_type") == "local_successor_capture_recovery_resumed"
        and evidence.get("capture_recovery_resumed") is True
        and evidence.get("reused_send_proof_kind")
        == "post_send_local_successor_v1"
        and evidence.get("external_action_performed") is False
        for event in events
    )


def test_browser_recovery_resumes_blocked_local_proof_capture_without_send(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project_id, interaction_id, adapter = _sent_local_unit_for_recovery(
        services,
        tmp_path,
        project_config_path,
        repository_root,
        recovery_user_id="wrong-local-user",
    )
    sent_before = tuple(adapter.sent_messages)
    first_report = BrowserRecovery(_service(services, adapter)).recover(project_id)
    assert _skipped_items(first_report)[0]["error_code"] == (
        "BROWSER_RECOVERY_UNIT_LOCAL_BINDING_INVALID"
    )
    adapter.recovery_user_id = None
    adapter.reject_legacy_inspection = True
    adapter.legacy_inspection_count = 0

    second_report = BrowserRecovery(_service(services, adapter)).recover(project_id)

    recovered = _recovered_items(second_report)
    assert len(recovered) == 1
    assert recovered[0]["status"] == "imported"
    assert adapter.legacy_inspection_count == 0
    assert adapter.sent_messages == list(sent_before)
    with services.database.connection() as connection:
        final = BrowserInteractionRepository(connection).get(interaction_id)
        run = StageRunRepository(connection).get(final.stage_run_id)
    assert final.status is InteractionStatus.IMPORTED
    assert final.response_artifact_id is not None
    assert run.status is RunStatus.DONE


def test_blocked_unit_without_send_observed_is_not_local_capture_recoverable(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id, interaction_id, adapter = _sent_local_unit_for_recovery(
        services,
        tmp_path,
        project_config_path,
        repository_root,
        recovery_user_id="wrong-local-user",
    )
    BrowserRecovery(_service(services, adapter)).recover(project_id)
    with (
        services.database.connection() as connection,
        services.database.transaction(connection),
    ):
        sent_event = next(
            event
            for event in BrowserInteractionRepository(connection).events(interaction_id)
            if event["status"] == InteractionStatus.SENT.value
        )
        evidence = sent_event["evidence"]
        assert isinstance(evidence, dict)
        evidence.pop("effect_boundary")
        connection.execute(
            "UPDATE browser_interaction_events SET evidence_json = ? WHERE id = ?",
            (
                json.dumps(evidence, sort_keys=True, separators=(",", ":")),
                sent_event["id"],
            ),
        )
    send_count_before = len(adapter.sent_messages)
    monkeypatch.setattr(
        adapter,
        "ensure_ready",
        lambda: (_ for _ in ()).throw(
            AssertionError("ineligible local capture recovery must not open the browser")
        ),
    )

    with pytest.raises(ConflictError) as captured:
        _service(services, adapter).reconcile_blocked_interaction(project_id, interaction_id)

    assert captured.value.code == "BROWSER_RECONCILE_SOURCE_INVALID"
    assert len(adapter.sent_messages) == send_count_before


def test_sent_unit_recovery_blocks_real_assistant_identity_mismatch(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project_id, interaction_id, adapter = _sent_local_unit_for_recovery(
        services,
        tmp_path,
        project_config_path,
        repository_root,
        sent_assistant_id="assistant-real-A",
        recovery_assistant_id="assistant-real-B",
    )
    sent_before = tuple(adapter.sent_messages)
    adapter.reject_legacy_inspection = True
    adapter.legacy_inspection_count = 0

    report = BrowserRecovery(_service(services, adapter)).recover(project_id)

    skipped = _skipped_items(report)
    assert len(skipped) == 1
    assert skipped[0]["error_code"] == "BROWSER_RECOVERY_UNIT_LOCAL_BINDING_INVALID"
    assert adapter.legacy_inspection_count == 0
    assert adapter.sent_messages == list(sent_before)
    with services.database.connection() as connection:
        final = BrowserInteractionRepository(connection).get(interaction_id)
        run = StageRunRepository(connection).get(final.stage_run_id)
        events = BrowserInteractionRepository(connection).events(interaction_id)
    assert final.status is InteractionStatus.BLOCKED
    assert run.status is RunStatus.BLOCKED
    assert run.finished_at is not None
    assert not any(
        isinstance(evidence := event.get("evidence"), dict)
        and evidence.get("event_type") == "assistant_successor_late_bound"
        for event in events
    )


def test_sent_unit_local_proof_recovery_missing_identity_blocks_and_finishes_run(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project_id, interaction_id, adapter = _sent_local_unit_for_recovery(
        services,
        tmp_path,
        project_config_path,
        repository_root,
        persist_selected_user_id=False,
    )
    sent_before = tuple(adapter.sent_messages)
    adapter.reject_legacy_inspection = True
    adapter.legacy_inspection_count = 0

    report = BrowserRecovery(_service(services, adapter)).recover(project_id)

    skipped = _skipped_items(report)
    assert len(skipped) == 1
    assert skipped[0]["error_code"] == "BROWSER_RECOVERY_UNIT_LOCAL_BINDING_INVALID"
    assert adapter.legacy_inspection_count == 0
    assert adapter.sent_messages == list(sent_before)
    with services.database.connection() as connection:
        final = BrowserInteractionRepository(connection).get(interaction_id)
        events = BrowserInteractionRepository(connection).events(interaction_id)
        run = StageRunRepository(connection).get(final.stage_run_id)
    assert final.status is InteractionStatus.BLOCKED
    assert final.response_artifact_id is None
    assert events[-1]["status"] == InteractionStatus.BLOCKED.value
    assert events[-1]["evidence"] == {
        "error_code": "BROWSER_RECOVERY_UNIT_LOCAL_BINDING_INVALID"
    }
    assert run.status is RunStatus.BLOCKED
    assert run.finished_at is not None


def test_sent_unit_without_local_proof_marker_keeps_legacy_recovery_path(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project_id, interaction_id, adapter = _sent_local_unit_for_recovery(
        services,
        tmp_path,
        project_config_path,
        repository_root,
    )
    with (
        services.database.connection() as connection,
        services.database.transaction(connection),
    ):
        connection.execute(
            "UPDATE browser_interaction_events SET evidence_json = ? "
            "WHERE interaction_id = ? AND status = 'sent'",
            ('{"effect_boundary":"send_observed"}', interaction_id),
        )
    sent_before = tuple(adapter.sent_messages)
    adapter.legacy_inspection_count = 0

    report = BrowserRecovery(_service(services, adapter)).recover(project_id)

    recovered = _recovered_items(report)
    assert len(recovered) == 1
    assert recovered[0]["status"] == "imported"
    assert adapter.legacy_inspection_count > 0
    assert adapter.sent_messages == list(sent_before)


@pytest.mark.parametrize("candidate_count", [0, 2])
def test_bootstrap_recovery_blocks_zero_or_multiple_candidates(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
    candidate_count: int,
) -> None:
    project_id, context_id = _automation_context(
        services, tmp_path, project_config_path, repository_root
    )
    adapter = FakeChatAdapter()
    with pytest.raises(RuntimeError):
        _service(services, adapter, "browser.after_send").run_context(project_id, context_id)
    with services.database.connection() as connection:
        interaction = connection.execute(
            "SELECT transport_fingerprint FROM browser_interactions WHERE context_id = ?",
            (context_id,),
        ).fetchone()
    assert interaction is not None
    fingerprint = str(interaction[0])
    adapter.bootstrap_override = tuple(
        BootstrapCandidate(
            f"/c/00000000-0000-4000-8000-{index:012d}",
            fingerprint,
            "2026-01-01T00:00:00+00:00",
        )
        for index in range(candidate_count)
    )
    report = BrowserRecovery(_service(services, adapter)).recover(project_id)
    assert _skipped_items(report)[0]["error_code"] == "BROWSER_BOOTSTRAP_AMBIGUOUS"
    assert len(adapter.sent_messages) == 1


def test_crash_after_m2_import_recovers_idempotently_and_session_failures_block(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project_id, context_id = _automation_context(
        services, tmp_path, project_config_path, repository_root
    )
    adapter = FakeChatAdapter()
    with pytest.raises(RuntimeError, match="browser.after_m2_import"):
        _service(services, adapter, "browser.after_m2_import").run_context(
            project_id, context_id
        )
    with services.database.connection() as connection:
        assert connection.execute(
            "SELECT count(*) FROM writing_acknowledgements WHERE context_id = ?", (context_id,)
        ).fetchone()[0] == 1
    recovered = BrowserRecovery(_service(services, adapter)).recover(project_id)
    assert _recovered_items(recovered)[0]["status"] == "imported"
    with services.database.connection() as connection:
        assert connection.execute(
            "SELECT count(*) FROM writing_acknowledgements WHERE context_id = ?", (context_id,)
        ).fetchone()[0] == 1

    project_id_2, context_id_2 = _automation_context(
        services, tmp_path / "other", project_config_path, repository_root
    )
    blocked_adapter = FakeChatAdapter()
    blocked_adapter.session_state = SessionState.CHALLENGE
    with pytest.raises(IntegrityError) as captured:
        _service(services, blocked_adapter).run_context(project_id_2, context_id_2)
    assert captured.value.code == "BROWSER_SECURITY_CHALLENGE"


def test_explicit_resume_import_completes_captured_unit_without_browser_io(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project_id, interaction_id, adapter, browser = _captured_unit_for_import_resume(
        services, tmp_path, project_config_path, repository_root
    )
    sent_before_resume = tuple(adapter.sent_messages)
    adapter.session_state = SessionState.CHALLENGE

    result = browser.resume_captured_import(project_id, interaction_id)

    assert result["outcome"] == "imported"
    assert result["status"] == "imported"
    assert result["preparation_id"] is not None
    assert adapter.sent_messages == list(sent_before_resume)
    with services.database.connection() as connection:
        interaction = BrowserInteractionRepository(connection).get(interaction_id)
        run = StageRunRepository(connection).get(interaction.stage_run_id)
        submission_count = connection.execute(
            "SELECT count(*) FROM text_unit_submissions WHERE id = ?",
            (interaction.imported_entity_id,),
        ).fetchone()[0]
    assert interaction.status is InteractionStatus.IMPORTED
    assert interaction.imported_at is not None
    assert run.status is RunStatus.DONE
    assert run.finished_at is not None
    assert submission_count == 1

    repeated = browser.resume_captured_import(project_id, interaction_id)
    assert repeated["outcome"] == "already_imported"
    assert repeated["imported_entity_id"] == interaction.imported_entity_id
    assert adapter.sent_messages == list(sent_before_resume)
    with services.database.connection() as connection:
        assert connection.execute(
            "SELECT count(*) FROM text_unit_submissions WHERE id = ?",
            (interaction.imported_entity_id,),
        ).fetchone()[0] == 1


def test_explicit_resume_import_fails_closed_for_state_and_preparation_drift(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project_id, interaction_id, adapter, browser = _captured_unit_for_import_resume(
        services, tmp_path, project_config_path, repository_root
    )
    with services.database.connection() as connection:
        interaction = BrowserInteractionRepository(connection).get(interaction_id)
    replacement = services.writing.prepare_unit_for_context(
        project_id, interaction.context_id, "START", reprocess=True
    )
    with (
        services.database.connection() as connection,
        services.database.transaction(connection),
    ):
        connection.execute(
            "UPDATE browser_interactions SET preparation_id = ? WHERE id = ?",
            (replacement.id, interaction.id),
        )
    adapter.session_state = SessionState.CHALLENGE
    with pytest.raises(IntegrityError) as divergent:
        browser.resume_captured_import(project_id, interaction.id)
    assert divergent.value.code == "BROWSER_IMPORT_RESUME_BINDING_INVALID"
    assert len(adapter.sent_messages) == 2

    with (
        services.database.connection() as connection,
        services.database.transaction(connection),
    ):
        connection.execute(
            "UPDATE browser_interactions SET preparation_id = ?, status = 'sent' WHERE id = ?",
            (interaction.preparation_id, interaction.id),
        )
    with pytest.raises(ConflictError) as invalid_state:
        browser.resume_captured_import(project_id, interaction.id)
    assert invalid_state.value.code == "BROWSER_IMPORT_RESUME_SOURCE_INVALID"
    assert len(adapter.sent_messages) == 2


@pytest.mark.parametrize(
    "checkpoint",
    ["browser.response_stored", "browser.response_captured", "browser.after_m2_import"],
)
def test_response_crash_matrix_recovers_without_duplicate_send_or_import(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
    checkpoint: str,
) -> None:
    project_id, context_id = _automation_context(
        services, tmp_path, project_config_path, repository_root
    )
    adapter = FakeChatAdapter()
    with pytest.raises(RuntimeError, match=checkpoint):
        _service(services, adapter, checkpoint).run_context(project_id, context_id)
    assert len(adapter.sent_messages) == 1
    result = BrowserRecovery(_service(services, adapter)).recover(project_id)
    assert _recovered_items(result)[0]["status"] == "imported"
    assert len(adapter.sent_messages) == 1
    with services.database.connection() as connection:
        assert connection.execute(
            "SELECT count(*) FROM writing_acknowledgements WHERE context_id = ?", (context_id,)
        ).fetchone()[0] == 1


def test_rejected_repair_is_explicit_limited_and_preserves_history(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project_id, context_id = _automation_context(
        services, tmp_path, project_config_path, repository_root
    )
    adapter = FakeChatAdapter()
    browser = _service(services, adapter)
    browser.run_context(project_id, context_id)
    with services.database.connection() as connection:
        acknowledgement = AcknowledgementRepository(connection).latest(context_id)
    assert acknowledgement is not None
    services.writing.confirm_context_by_id(project_id, context_id, acknowledgement.raw_version)

    adapter.next_response = "curto"
    first = browser.run_unit(project_id, "START", context_id=context_id)
    assert first["disposition"] == "rejected" and first["paused"] is True
    adapter.next_response = "Texto reparado com extensão objetiva suficiente para aceitação direta."
    repaired = browser.retry_rejected(project_id, str(first["id"]))
    assert repaired["disposition"] == "accepted"
    assert repaired["status"] == "imported"
    assert "WRITING_CHARACTER_COUNT_INVALID" in adapter.sent_messages[-1][1]
    with services.database.connection() as connection:
        assert connection.execute(
            "SELECT count(*) FROM text_unit_submissions WHERE context_id = ? AND unit_id = 'START'",
            (context_id,),
        ).fetchone()[0] == 2
        assert connection.execute(
            "SELECT count(*) FROM browser_interactions WHERE context_id = ? AND unit_id = 'START'",
            (context_id,),
        ).fetchone()[0] == 2


@pytest.mark.parametrize(
    ("state", "code"),
    [
        (SessionState.LOGIN_REQUIRED, "BROWSER_LOGIN_REQUIRED"),
        (SessionState.EXPIRED, "BROWSER_SESSION_EXPIRED"),
        (SessionState.CHALLENGE, "BROWSER_SECURITY_CHALLENGE"),
    ],
)
def test_session_gates_block_without_send(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
    state: SessionState,
    code: str,
) -> None:
    project_id, context_id = _automation_context(
        services, tmp_path, project_config_path, repository_root
    )
    adapter = FakeChatAdapter()
    adapter.session_state = state
    with pytest.raises(IntegrityError) as captured:
        _service(services, adapter).run_context(project_id, context_id)
    assert captured.value.code == code
    assert adapter.sent_messages == []


def test_capture_gate_wrong_conversation_empty_response_and_artifact_corruption_fail_closed(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    project_id, context_id = _automation_context(
        services, tmp_path, project_config_path, repository_root
    )
    adapter = FakeChatAdapter()
    adapter.gate_ready = False
    with pytest.raises(IntegrityError) as gate:
        _service(services, adapter).run_context(project_id, context_id)
    assert gate.value.code == "BROWSER_CAPTURE_SPIKE_REQUIRED"
    assert adapter.sent_messages == []

    project_id_2, context_id_2 = _automation_context(
        services, tmp_path / "other", project_config_path, repository_root
    )
    adapter_2 = FakeChatAdapter()
    adapter_2.next_path = "/c/595c9f06-2989-4b10-a517-db295e0872cc"
    adapter_2.next_response = ""
    with pytest.raises(IntegrityError) as empty:
        _service(services, adapter_2).run_context(project_id_2, context_id_2)
    assert empty.value.code == "BROWSER_RESPONSE_EMPTY"

    # A complete successful capture becomes invalid if its persisted bytes are changed.
    project_id_3, context_id_3 = _automation_context(
        services, tmp_path / "third", project_config_path, repository_root
    )
    adapter_3 = FakeChatAdapter()
    adapter_3.next_path = "/c/b529d37f-b227-42d2-93d8-3b0594d7f315"
    _service(services, adapter_3).run_context(project_id_3, context_id_3)
    with services.database.connection() as connection:
        row = connection.execute(
            "SELECT a.relative_path, p.artifact_root FROM browser_interactions i "
            "JOIN artifacts a ON a.id = i.response_artifact_id "
            "JOIN projects p ON p.id = i.project_id WHERE i.context_id = ?",
            (context_id_3,),
        ).fetchone()
    assert row is not None
    services.store.resolve(str(row["artifact_root"]), str(row["relative_path"])).write_bytes(
        b"corrupted"
    )
    issues = _service(services, adapter_3).validate(project_id_3)
    assert any(issue.code == "BROWSER_RESPONSE_ARTIFACT_INVALID" for issue in issues)


def test_wrong_conversation_and_timeout_block_instead_of_resending(
    services: ServiceBundle,
    tmp_path: Path,
    project_config_path: Path,
    repository_root: Path,
) -> None:
    wrong_project, wrong_context = _automation_context(
        services, tmp_path / "wrong", project_config_path, repository_root
    )
    wrong = FakeChatAdapter()
    wrong.next_path = "/c/c93db77c-0c16-4379-a499-f0c966d46703"
    wrong.inspection_path_override = "/c/a15a3ccb-b67a-4553-aa62-a5407ff4d5f7"
    with pytest.raises(IntegrityError) as mismatch:
        _service(services, wrong).run_context(wrong_project, wrong_context)
    assert mismatch.value.code == "BROWSER_WRONG_CONVERSATION"
    assert len(wrong.sent_messages) == 1

    timeout_project, timeout_context = _automation_context(
        services, tmp_path / "timeout", project_config_path, repository_root
    )
    streaming = FakeChatAdapter()
    streaming.next_path = "/c/d08c5621-f03f-4332-a72d-b4bea5e73ef4"
    streaming.inspect_override = TurnState.STREAMING
    zero_timeout_config = services.config.model_copy(
        update={"browser_timeout_seconds": 0}
    )
    timeout_service = BrowserAutomationService(
        zero_timeout_config,
        services.database,
        services.store,
        streaming,
        services.writing,
    )
    with pytest.raises(IntegrityError) as timed_out:
        timeout_service.run_context(timeout_project, timeout_context)
    assert timed_out.value.code == "BROWSER_RESPONSE_TIMEOUT"
    assert len(streaming.sent_messages) == 1

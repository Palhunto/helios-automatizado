from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime
from time import monotonic, sleep
from typing import NoReturn

from ebook_pipeline.browser.fingerprints import (
    captured_response_bytes,
    strict_request_text,
    transport_fingerprint,
)
from ebook_pipeline.browser.models import (
    BrowserConversation,
    BrowserInteraction,
    BrowserInteractionResolution,
    ConversationResolutionAction,
    ConversationStatus,
    InteractionKind,
    InteractionResolutionKind,
    InteractionStatus,
    SessionState,
    TurnState,
)
from ebook_pipeline.browser.persistence import BrowserPersistence
from ebook_pipeline.browser.ports import ChatProviderAdapter
from ebook_pipeline.browser.repositories import (
    BrowserConversationInvalidationRepository,
    BrowserConversationRepository,
    BrowserInteractionRepository,
    BrowserInteractionResolutionRepository,
)
from ebook_pipeline.browser.urls import is_real_conversation_path
from ebook_pipeline.config import AppConfig
from ebook_pipeline.core.errors import ConflictError, HeliosError, IntegrityError, NotFoundError
from ebook_pipeline.core.hashing import sha256_bytes
from ebook_pipeline.core.ids import new_id, utc_now
from ebook_pipeline.core.models import Artifact, RunStatus, ValidationIssue
from ebook_pipeline.core.runtime_config import ProjectRuntimeService
from ebook_pipeline.storage.artifacts import ArtifactStore
from ebook_pipeline.storage.database import Database
from ebook_pipeline.storage.repositories import (
    ArtifactRepository,
    ErrorRepository,
    ProjectRepository,
    StageRunRepository,
)
from ebook_pipeline.writing.models import (
    ProductionUnitStatus,
    SubmissionDisposition,
)
from ebook_pipeline.writing.repositories import PreparationRepository, SubmissionRepository
from ebook_pipeline.writing.service import WritingService

BrowserFaultHook = Callable[[str], None]
LOGGER = logging.getLogger(__name__)
AUTOMATION_CONTRACT_VERSION = 4
AUTOMATION_REQUEST_PROMPT_VERSION = 2
ACTIVE_INTERACTION_STATUSES = frozenset(
    {
        InteractionStatus.PREPARED,
        InteractionStatus.SENDING,
        InteractionStatus.SENT,
        InteractionStatus.STREAMING,
        InteractionStatus.CAPTURED,
    }
)
POST_SEND_RECONCILIATION_ERROR_CODES = frozenset(
    {"BROWSER_SEND_REQUIRES_RECONCILE"}
)
EXPLICIT_RECONCILIATION_ERROR_CODES = frozenset(
    {"BROWSER_SEND_NOT_PROVABLE", "BROWSER_SEND_REQUIRES_RECONCILE"}
)
UNIT_RECONCILIATION_HYDRATION_STALL_SECONDS = 5.0
REQUEST_PLACEHOLDER_ASSISTANT_PREFIX = "request-placeholder-"


class BrowserAutomationService:
    def __init__(
        self,
        config: AppConfig,
        database: Database,
        store: ArtifactStore,
        adapter: ChatProviderAdapter,
        writing: WritingService | None = None,
        *,
        fault_hook: BrowserFaultHook | None = None,
    ) -> None:
        self.config = config
        self.database = database
        self.store = store
        self.adapter = adapter
        self.writing = writing or WritingService(config, database, store)
        self.persistence = BrowserPersistence(config)
        self.runtime_config = ProjectRuntimeService(config, database, store)
        self.fault_hook = fault_hook

    def prepare_context(
        self, project_id: str, context_id: str | None = None
    ) -> BrowserInteraction:
        self._assert_browser_enabled(project_id)
        if context_id is None:
            try:
                context = self.writing.context(project_id)
            except NotFoundError:
                context = self.writing.create_context(
                    project_id,
                    contract_id="omega_writing_production",
                    contract_version=AUTOMATION_CONTRACT_VERSION,
                    request_prompt_id="helios_writing_unit_request",
                    request_prompt_version=AUTOMATION_REQUEST_PROMPT_VERSION,
                )
            if (
                context.contract_version != AUTOMATION_CONTRACT_VERSION
                or context.request_prompt_version != AUTOMATION_REQUEST_PROMPT_VERSION
            ):
                context = self.writing.create_context(
                    project_id,
                    contract_id="omega_writing_production",
                    contract_version=AUTOMATION_CONTRACT_VERSION,
                    request_prompt_id="helios_writing_unit_request",
                    request_prompt_version=AUTOMATION_REQUEST_PROMPT_VERSION,
                )
        else:
            context = self.writing.contexts.get_by_id(project_id, context_id)
        self._assert_automation_context(project_id, context.id)
        context, content = self.writing.context_package_by_id(project_id, context.id)
        if context.package_artifact_id is None:
            raise IntegrityError(
                "WRITING_CONTEXT_ARTIFACT_MISSING", "Context package is not registered"
            )
        text = strict_request_text(content)
        fingerprint = transport_fingerprint(text)
        with (
            self.database.connection() as connection,
            self.database.transaction(connection),
        ):
            conversation = self.persistence.conversation(connection, project_id, context.id)
            return self.persistence.create_interaction(
                    connection,
                    project_id=project_id,
                    conversation_id=conversation.id,
                    context_id=context.id,
                    kind=InteractionKind.CONTEXT_LOAD,
                    request_artifact_id=context.package_artifact_id,
                    request_sha256=context.package_sha256,
                    transport_fingerprint=fingerprint,
            )

    def prepare_unit(
        self,
        project_id: str,
        unit_id: str,
        *,
        context_id: str | None = None,
        reprocess: bool = False,
    ) -> BrowserInteraction:
        self._assert_browser_enabled(project_id)
        context = (
            self.writing.context(project_id)
            if context_id is None
            else self.writing.contexts.get_by_id(project_id, context_id)
        )
        self._assert_automation_context(project_id, context.id)
        with self.database.connection() as connection:
            conversation = BrowserConversationRepository(connection).for_context(context.id)
            if conversation is None or conversation.status is not ConversationStatus.READY:
                raise ConflictError(
                    "BROWSER_CONTEXT_CONVERSATION_NOT_READY",
                    "Context must be loaded into a proven conversation before unit automation",
                )
            context_load = BrowserInteractionRepository(connection).latest_for_context_kind(
                context.id, InteractionKind.CONTEXT_LOAD
            )
            if context_load is None or context_load.status is not InteractionStatus.IMPORTED:
                raise ConflictError(
                    "BROWSER_CONTEXT_LOAD_INCOMPLETE",
                    "Context acknowledgement must be captured before unit automation",
                )
        preparation = self.writing.prepare_unit_for_context(
            project_id, context.id, unit_id, reprocess=reprocess
        )
        preparation, content = self.writing.unit_request_by_id(project_id, preparation.id)
        if preparation.request_artifact_id is None:
            raise IntegrityError(
                "WRITING_PREPARATION_ARTIFACT_MISSING", "Unit request is not registered"
            )
        text = strict_request_text(content)
        with self.database.connection() as connection:
            conversation = BrowserConversationRepository(connection).for_context(context.id)
            assert conversation is not None
            with self.database.transaction(connection):
                return self.persistence.create_interaction(
                    connection,
                    project_id=project_id,
                    conversation_id=conversation.id,
                    context_id=context.id,
                    kind=InteractionKind.UNIT_REQUEST,
                    unit_id=preparation.unit_id,
                    preparation_id=preparation.id,
                    request_artifact_id=preparation.request_artifact_id,
                    request_sha256=preparation.request_sha256,
                    transport_fingerprint=transport_fingerprint(text),
                )

    def run_context(self, project_id: str, context_id: str | None = None) -> dict[str, object]:
        item = self.prepare_context(project_id, context_id)
        return self.run_interaction(item.id)

    def run_unit(
        self, project_id: str, unit_id: str, *, context_id: str | None = None
    ) -> dict[str, object]:
        item = self.prepare_unit(project_id, unit_id, context_id=context_id)
        return self.run_interaction(item.id)

    def continue_project(self, project_id: str) -> dict[str, object]:
        """Advance at most one proven operation and pause at every human/domain gate."""
        context = self.writing.context(project_id)
        with self.database.connection() as connection:
            interaction_repository = BrowserInteractionRepository(connection)
            resolution_repository = BrowserInteractionResolutionRepository(connection)
            incomplete = interaction_repository.list_incomplete(project_id)
            unresolved_blocked = [
                item
                for item in interaction_repository.list_blocked(project_id)
                if resolution_repository.for_interaction(item.id) is None
            ]
            conversation = BrowserConversationRepository(connection).for_context(context.id)
            context_load = interaction_repository.latest_for_context_kind(
                context.id, InteractionKind.CONTEXT_LOAD
            )
        if unresolved_blocked:
            raise ConflictError(
                "BROWSER_INTERACTION_BLOCKED",
                "Blocked browser interaction requires explicit operator resolution",
                evidence={"interaction_id": unresolved_blocked[-1].id},
            )
        if incomplete:
            raise ConflictError(
                "BROWSER_RECOVERY_REQUIRED",
                "Recover the incomplete browser interaction before continuing",
            )
        if conversation is None or context_load is None:
            return self.run_context(project_id, context.id)
        if context_load.status is InteractionStatus.BLOCKED:
            return self.run_context(project_id, context.id)
        if context_load.status is not InteractionStatus.IMPORTED:
            raise ConflictError(
                "BROWSER_CONTEXT_LOAD_INCOMPLETE", "Context load is not imported"
            )
        with self.database.connection() as connection:
            confirmed = connection.execute(
                "SELECT 1 FROM writing_acknowledgements "
                "WHERE context_id = ? AND confirmed_at IS NOT NULL",
                (context.id,),
            ).fetchone()
        if confirmed is None:
            return {
                "project_id": project_id,
                "status": "paused",
                "reason": "context_confirmation_required",
            }
        production_set = self.writing.production_set(project_id)
        writing_status = self.writing.status(project_id)
        production_payload = writing_status.get("production_set")
        latest_by_unit: dict[str, str | None] = {}
        if isinstance(production_payload, dict):
            units = production_payload.get("units")
            if isinstance(units, list):
                latest_by_unit = {
                    str(unit["unit_id"]): (
                        None
                        if unit.get("latest_disposition") is None
                        else str(unit["latest_disposition"])
                    )
                    for unit in units
                    if isinstance(unit, dict) and "unit_id" in unit
                }
        for selection in production_set.selections:
            if selection.status.value == "current_compatible":
                continue
            disposition = latest_by_unit.get(selection.unit_id)
            if disposition in {"review_required", "rejected"}:
                return {
                    "project_id": project_id,
                    "status": "paused",
                    "reason": disposition,
                    "unit_id": selection.unit_id,
                }
            return self.run_unit(project_id, selection.unit_id, context_id=context.id)
        return {"project_id": project_id, "status": "complete"}

    def run_writing(self, project_id: str) -> dict[str, object]:
        """Run eligible writing units sequentially in this service's browser session."""
        self._assert_browser_enabled(project_id)
        with self.database.connection() as connection:
            ProjectRepository(connection).get(project_id)

        processed_units: list[str] = []
        recovered_interactions: list[str] = []
        accepted_units: list[str] = []
        recorded_warning_submission_ids: set[str] = set()
        warning_unit_order: list[str] = []
        warning_counts_by_unit: dict[str, int] = {}
        warning_codes_by_unit: dict[str, list[str]] = {}

        def summary(
            stop_reason: str,
            *,
            review_required_unit: str | None = None,
            blocked_interaction: str | None = None,
            error_code: str | None = None,
        ) -> dict[str, object]:
            result: dict[str, object] = {
                "project_id": project_id,
                "processed_units": processed_units,
                "recovered_interactions": recovered_interactions,
                "accepted_units": accepted_units,
                "warning_count": sum(warning_counts_by_unit.values()),
                "warning_units": [
                    {
                        "unit_id": unit_id,
                        "warning_count": warning_counts_by_unit[unit_id],
                        "codes": warning_codes_by_unit[unit_id],
                    }
                    for unit_id in warning_unit_order
                ],
                "review_required_unit": review_required_unit,
                "blocked_interaction": blocked_interaction,
                "stop_reason": stop_reason,
            }
            if error_code is not None:
                result["error_code"] = error_code
            return result

        while True:
            blocked = self._unresolved_unit_interactions(
                project_id, status=InteractionStatus.BLOCKED
            )
            if blocked:
                item = blocked[0]
                if len(blocked) > 1 or not self._interaction_has_external_effect(item):
                    return summary("blocked", blocked_interaction=item.id)
                try:
                    reconciled = self.reconcile_blocked_interaction(project_id, item.id)
                except HeliosError as exc:
                    return summary(
                        "blocked",
                        blocked_interaction=item.id,
                        error_code=exc.code,
                    )
                recovered_interactions.append(item.id)
                try:
                    stop = self._record_writing_batch_result(
                        project_id,
                        reconciled,
                        processed_units=processed_units,
                        accepted_units=accepted_units,
                        recorded_warning_submission_ids=recorded_warning_submission_ids,
                        warning_unit_order=warning_unit_order,
                        warning_counts_by_unit=warning_counts_by_unit,
                        warning_codes_by_unit=warning_codes_by_unit,
                    )
                except HeliosError as exc:
                    return summary(
                        "blocked",
                        blocked_interaction=item.id,
                        error_code=exc.code,
                    )
                if stop is not None:
                    return summary(
                        stop,
                        blocked_interaction=(item.id if stop == "blocked" else None),
                    )
                continue

            active = self._active_unit_interactions(project_id)
            if active:
                item = active[0]
                if len(active) > 1:
                    return summary("blocked", blocked_interaction=item.id)
                result, error_code = self._run_batch_interaction(project_id, item)
                if result is None:
                    return summary(
                        "blocked",
                        blocked_interaction=item.id,
                        error_code=error_code,
                    )
                recovered_interactions.append(item.id)
                try:
                    stop = self._record_writing_batch_result(
                        project_id,
                        result,
                        processed_units=processed_units,
                        accepted_units=accepted_units,
                        recorded_warning_submission_ids=recorded_warning_submission_ids,
                        warning_unit_order=warning_unit_order,
                        warning_counts_by_unit=warning_counts_by_unit,
                        warning_codes_by_unit=warning_codes_by_unit,
                    )
                except HeliosError as exc:
                    return summary(
                        "blocked",
                        blocked_interaction=item.id,
                        error_code=exc.code,
                    )
                if stop is not None:
                    return summary(
                        stop,
                        blocked_interaction=(item.id if stop == "blocked" else None),
                    )
                continue

            context = self.writing.context(project_id)
            production_set = self.writing.production_set(project_id)
            if production_set.complete:
                return summary("complete")

            writing_status = self.writing.status(project_id)
            production_payload = writing_status.get("production_set")
            latest_dispositions: dict[str, str | None] = {}
            if isinstance(production_payload, dict):
                units = production_payload.get("units")
                if isinstance(units, list):
                    latest_dispositions = {
                        str(unit["unit_id"]): (
                            None
                            if unit.get("latest_disposition") is None
                            else str(unit["latest_disposition"])
                        )
                        for unit in units
                        if isinstance(unit, dict) and "unit_id" in unit
                    }

            current_units = {
                selection.unit_id
                for selection in production_set.selections
                if selection.status is ProductionUnitStatus.CURRENT_COMPATIBLE
            }
            contract = self.writing.contexts.resolve_contract(context).model
            next_unit_id: str | None = None
            for unit in contract.ordered_units():
                selection = next(
                    candidate
                    for candidate in production_set.selections
                    if candidate.unit_id == unit.unit_id
                )
                if selection.status is not ProductionUnitStatus.MISSING_ACCEPTED:
                    continue
                disposition = latest_dispositions.get(unit.unit_id)
                if disposition == SubmissionDisposition.REVIEW_REQUIRED.value:
                    try:
                        result = self._pending_browser_review_result(
                            project_id, context.id, unit.unit_id
                        )
                        stop = self._record_writing_batch_result(
                            project_id,
                            result,
                            processed_units=processed_units,
                            accepted_units=accepted_units,
                            recorded_warning_submission_ids=(
                                recorded_warning_submission_ids
                            ),
                            warning_unit_order=warning_unit_order,
                            warning_counts_by_unit=warning_counts_by_unit,
                            warning_codes_by_unit=warning_codes_by_unit,
                        )
                    except HeliosError as exc:
                        return summary("blocked", error_code=exc.code)
                    if stop is not None:
                        return summary(stop)
                    break
                if disposition == SubmissionDisposition.REJECTED.value:
                    return summary("rejected")
                if set(unit.continuity_from).issubset(current_units):
                    next_unit_id = unit.unit_id
                    break
            else:
                if next_unit_id is None:
                    return summary("no_eligible_unit")
            if next_unit_id is None:
                # A browser-imported warning-only review was accepted above. Restart from a
                # freshly resolved Production Set before preparing any dependent unit.
                continue

            interaction = self.prepare_unit(
                project_id, next_unit_id, context_id=context.id
            )
            result, error_code = self._run_batch_interaction(project_id, interaction)
            if result is None:
                return summary(
                    "blocked",
                    blocked_interaction=interaction.id,
                    error_code=error_code,
                )
            if result.get("outcome") == "reconciled":
                recovered_interactions.append(interaction.id)
            try:
                stop = self._record_writing_batch_result(
                    project_id,
                    result,
                    processed_units=processed_units,
                    accepted_units=accepted_units,
                    recorded_warning_submission_ids=recorded_warning_submission_ids,
                    warning_unit_order=warning_unit_order,
                    warning_counts_by_unit=warning_counts_by_unit,
                    warning_codes_by_unit=warning_codes_by_unit,
                )
            except HeliosError as exc:
                return summary(
                    "blocked",
                    blocked_interaction=interaction.id,
                    error_code=exc.code,
                )
            if stop is not None:
                return summary(
                    stop,
                    blocked_interaction=(
                        interaction.id if stop == "blocked" else None
                    ),
                )

    def _unresolved_unit_interactions(
        self, project_id: str, *, status: InteractionStatus
    ) -> list[BrowserInteraction]:
        with self.database.connection() as connection:
            interactions = BrowserInteractionRepository(connection)
            resolutions = BrowserInteractionResolutionRepository(connection)
            return [
                item
                for item in interactions.list_for_project(project_id)
                if item.kind is InteractionKind.UNIT_REQUEST
                and item.status is status
                and resolutions.for_interaction(item.id) is None
            ]

    def _active_unit_interactions(self, project_id: str) -> list[BrowserInteraction]:
        with self.database.connection() as connection:
            return [
                item
                for item in BrowserInteractionRepository(connection).list_for_project(
                    project_id
                )
                if item.kind is InteractionKind.UNIT_REQUEST
                and item.status in ACTIVE_INTERACTION_STATUSES
            ]

    def _interaction_has_external_effect(self, item: BrowserInteraction) -> bool:
        with self.database.connection() as connection:
            events = BrowserInteractionRepository(connection).events(item.id)
        return self._has_send_attempt_started_event(events)

    def _run_batch_interaction(
        self, project_id: str, item: BrowserInteraction
    ) -> tuple[dict[str, object] | None, str | None]:
        try:
            return self.run_interaction(item.id), None
        except HeliosError as exc:
            with self.database.connection() as connection:
                current = BrowserInteractionRepository(connection).get(item.id)
            if (
                current.status is InteractionStatus.BLOCKED
                and self._interaction_has_external_effect(current)
            ):
                try:
                    return self.reconcile_blocked_interaction(project_id, current.id), None
                except HeliosError as reconcile_error:
                    return None, reconcile_error.code
            return None, exc.code

    def _record_writing_batch_result(
        self,
        project_id: str,
        result: dict[str, object],
        *,
        processed_units: list[str],
        accepted_units: list[str],
        recorded_warning_submission_ids: set[str],
        warning_unit_order: list[str],
        warning_counts_by_unit: dict[str, int],
        warning_codes_by_unit: dict[str, list[str]],
    ) -> str | None:
        nested = result.get("result")
        imported = nested if isinstance(nested, dict) else result
        unit_value = imported.get("unit_id")
        disposition_value = imported.get("disposition")
        submission_id = imported.get("imported_entity_id")
        interaction_id = imported.get("id")
        if not all(
            isinstance(value, str)
            for value in (
                unit_value,
                disposition_value,
                submission_id,
                interaction_id,
            )
        ):
            return "blocked"
        assert isinstance(unit_value, str)
        assert isinstance(disposition_value, str)
        assert isinstance(submission_id, str)
        assert isinstance(interaction_id, str)
        with self.database.connection() as connection:
            interaction = BrowserInteractionRepository(connection).get(interaction_id)
            submission = SubmissionRepository(connection).get(submission_id)
        if (
            interaction.project_id != project_id
            or interaction.kind is not InteractionKind.UNIT_REQUEST
            or interaction.status is not InteractionStatus.IMPORTED
            or interaction.imported_entity_type != "text_unit_submission"
            or interaction.imported_entity_id != submission.id
            or interaction.context_id != submission.context_id
            or interaction.preparation_id != submission.preparation_id
            or interaction.unit_id != submission.unit_id
            or submission.project_id != project_id
            or submission.unit_id != unit_value
            or submission.disposition.value != disposition_value
        ):
            raise IntegrityError(
                "BROWSER_WRITING_BATCH_IMPORT_BINDING_INVALID",
                "Imported writing result differs from its browser interaction or M2 submission",
            )
        if unit_value not in processed_units:
            processed_units.append(unit_value)
        report = self.writing.unit_validation_report(project_id, submission)
        self._record_batch_warnings(
            submission.id,
            unit_value,
            report,
            recorded_submission_ids=recorded_warning_submission_ids,
            warning_unit_order=warning_unit_order,
            warning_counts_by_unit=warning_counts_by_unit,
            warning_codes_by_unit=warning_codes_by_unit,
        )
        error_count = report.get("error_count")
        if not isinstance(error_count, int) or error_count < 0:
            raise IntegrityError(
                "BROWSER_WRITING_ERROR_REPORT_INVALID",
                "M2 validation report lacks a valid error count",
            )
        if error_count > 0:
            if disposition_value != SubmissionDisposition.REJECTED.value:
                raise IntegrityError(
                    "BROWSER_WRITING_ERROR_DISPOSITION_DIVERGENT",
                    "M2 validation errors are not represented by a rejected disposition",
                )
            return "rejected"
        if disposition_value == SubmissionDisposition.ACCEPTED.value:
            if unit_value not in accepted_units:
                accepted_units.append(unit_value)
            return None
        if disposition_value == SubmissionDisposition.REVIEW_REQUIRED.value:
            warning_count = report.get("warning_count")
            if not isinstance(warning_count, int) or warning_count <= 0:
                raise IntegrityError(
                    "BROWSER_WRITING_REVIEW_NOT_WARNING_ONLY",
                    "Browser batch review is not a warning-only M2 validation result",
                )
            confirmed = self.writing.confirm_unit(
                project_id, submission.unit_id, submission.raw_version
            )
            if (
                confirmed.id != submission.id
                or confirmed.raw_version != submission.raw_version
                or confirmed.raw_sha256 != submission.raw_sha256
                or confirmed.disposition is not SubmissionDisposition.ACCEPTED
                or confirmed.accepted_artifact_id is None
            ):
                raise IntegrityError(
                    "BROWSER_WRITING_AUTO_CONFIRM_DIVERGENT",
                    "M2 confirmation did not accept the exact browser-imported raw version",
                )
            if unit_value not in accepted_units:
                accepted_units.append(unit_value)
            self.writing.production_set(project_id)
            return None
        if disposition_value == SubmissionDisposition.REJECTED.value:
            raise IntegrityError(
                "BROWSER_WRITING_REJECTED_WITHOUT_ERRORS",
                "Rejected M2 submission has no validation errors",
            )
        return "blocked"

    def _pending_browser_review_result(
        self, project_id: str, context_id: str, unit_id: str
    ) -> dict[str, object]:
        with self.database.connection() as connection:
            submission = SubmissionRepository(connection).latest(context_id, unit_id)
            if (
                submission is None
                or submission.project_id != project_id
                or submission.disposition is not SubmissionDisposition.REVIEW_REQUIRED
            ):
                raise IntegrityError(
                    "BROWSER_WRITING_PENDING_REVIEW_DIVERGENT",
                    "Pending writing review differs from the current M2 context",
                )
            candidates = [
                item
                for item in BrowserInteractionRepository(connection).list_for_project(project_id)
                if item.kind is InteractionKind.UNIT_REQUEST
                and item.status is InteractionStatus.IMPORTED
                and item.context_id == context_id
                and item.unit_id == unit_id
                and item.imported_entity_type == "text_unit_submission"
                and item.imported_entity_id == submission.id
            ]
        if len(candidates) != 1:
            raise ConflictError(
                "BROWSER_WRITING_REVIEW_NOT_AUTOCONFIRMABLE",
                "Warning review is not bound to exactly one imported browser interaction",
                evidence={"candidate_count": len(candidates), "unit_id": unit_id},
            )
        result = self._payload(candidates[0])
        result["disposition"] = submission.disposition.value
        return result

    @staticmethod
    def _record_batch_warnings(
        submission_id: str,
        unit_id: str,
        report: dict[str, object],
        *,
        recorded_submission_ids: set[str],
        warning_unit_order: list[str],
        warning_counts_by_unit: dict[str, int],
        warning_codes_by_unit: dict[str, list[str]],
    ) -> None:
        if submission_id in recorded_submission_ids:
            return
        warning_count = report.get("warning_count")
        findings = report.get("findings")
        if not isinstance(warning_count, int) or not isinstance(findings, list):
            raise IntegrityError(
                "BROWSER_WRITING_WARNING_REPORT_INVALID",
                "M2 validation report lacks warning findings or count",
            )
        codes: list[str] = []
        actual_warning_count = 0
        for finding in findings:
            if not isinstance(finding, dict):
                raise IntegrityError(
                    "BROWSER_WRITING_WARNING_REPORT_INVALID",
                    "M2 validation report contains an invalid finding",
                )
            if finding.get("severity") != "warning":
                continue
            code = finding.get("code")
            if not isinstance(code, str):
                raise IntegrityError(
                    "BROWSER_WRITING_WARNING_REPORT_INVALID",
                    "M2 validation report contains a warning without a code",
                )
            actual_warning_count += 1
            if code not in codes:
                codes.append(code)
        if actual_warning_count != warning_count:
            raise IntegrityError(
                "BROWSER_WRITING_WARNING_REPORT_DIVERGENT",
                "M2 validation report warning count differs from its findings",
            )
        recorded_submission_ids.add(submission_id)
        if warning_count == 0:
            return
        if unit_id not in warning_counts_by_unit:
            warning_unit_order.append(unit_id)
            warning_counts_by_unit[unit_id] = 0
            warning_codes_by_unit[unit_id] = []
        warning_counts_by_unit[unit_id] += warning_count
        for code in codes:
            if code not in warning_codes_by_unit[unit_id]:
                warning_codes_by_unit[unit_id].append(code)

    def retry_rejected(self, project_id: str, interaction_id: str) -> dict[str, object]:
        """Create one explicit, bounded repair turn; never edit the rejected response bytes."""
        with self.database.connection() as connection:
            project = ProjectRepository(connection).get(project_id)
            previous = BrowserInteractionRepository(connection).get(interaction_id)
            if (
                previous.project_id != project.id
                or previous.kind is not InteractionKind.UNIT_REQUEST
            ):
                raise NotFoundError(
                    "BROWSER_INTERACTION_NOT_FOUND", "Rejected unit interaction was not found"
                )
            if previous.status is not InteractionStatus.IMPORTED:
                raise ConflictError(
                    "BROWSER_RETRY_SOURCE_INCOMPLETE", "Only an imported rejection can be retried"
                )
            if previous.attempt >= previous.max_attempts:
                raise ConflictError(
                    "BROWSER_ATTEMPTS_EXHAUSTED", "Browser interaction retry limit was reached"
                )
            if previous.imported_entity_id is None:
                raise IntegrityError(
                    "BROWSER_IMPORT_EVIDENCE_MISSING", "Imported submission identity is missing"
                )
            submission = SubmissionRepository(connection).get(previous.imported_entity_id)
            if submission.disposition is not SubmissionDisposition.REJECTED:
                raise ConflictError(
                    "BROWSER_RETRY_NOT_REJECTED", "Only rejected submissions use repair retry"
                )
            conversation = BrowserConversationRepository(connection).get(
                previous.conversation_id
            )
        report = self.writing.unit_validation_report(project_id, submission)
        findings = report.get("findings")
        if not isinstance(findings, list):
            raise IntegrityError(
                "WRITING_VALIDATION_REPORT_INVALID", "Validation findings are unavailable"
            )
        whitelist = {
            "WRITING_CONTENT_EMPTY",
            "WRITING_CHARACTER_COUNT_INVALID",
            "WRITING_REQUIRED_HEADING_MISSING",
            "WRITING_FORBIDDEN_HEADING_PRESENT",
            "WRITING_FINAL_HEADING_INVALID",
            "WRITING_LIST_SYNTAX_FORBIDDEN",
            "WRITING_FORBIDDEN_PHRASE_PRESENT",
        }
        errors = [
            item
            for item in findings
            if isinstance(item, dict) and item.get("severity") == "error"
        ]
        codes = {str(item.get("code")) for item in errors}
        if not errors or not codes.issubset(whitelist):
            raise ConflictError(
                "BROWSER_REPAIR_NOT_DETERMINISTIC",
                "Rejected findings are outside the deterministic repair whitelist",
                evidence={"codes": sorted(codes)},
            )
        assert previous.unit_id is not None and previous.preparation_id is not None
        lines = [
            "A resposta anterior foi rejeitada por invariantes objetivas.",
            f"Reescreva exclusivamente a unidade {previous.unit_id}, corrigindo:",
        ]
        ordered_errors = sorted(errors, key=lambda value: str(value.get("code")))
        lines.extend(
            f"- {item['code']}: {item.get('message', '')}" for item in ordered_errors
        )
        lines.append("Entregue somente o texto integral revisado da unidade.")
        request = ("\n".join(lines) + "\n").encode("utf-8")
        digest = sha256_bytes(request)
        attempt = previous.attempt
        attempt_ordinal = attempt + 1
        run = self.persistence.run_states.new_run(
            project_id=project.id,
            stage_id="chatgpt_browser_automation",
            unit_id=f"unit:{previous.unit_id}",
            input_hash=digest,
            max_attempts=self.config.max_attempts,
            version=attempt_ordinal,
            supersedes_run_id=previous.stage_run_id,
        )
        stored = self.store.write_bytes(
            project.artifact_root,
            f"browser/requests/{previous.unit_id}/repair-{attempt_ordinal:04d}.txt",
            request,
        )
        with (
            self.database.connection() as connection,
            self.database.transaction(connection),
        ):
            StageRunRepository(connection).add(run)
            artifact = ArtifactRepository(connection).add_idempotent(
                    Artifact(
                        id=new_id(),
                        project_id=project.id,
                        stage_run_id=run.id,
                        artifact_type="browser_repair_request",
                        relative_path=stored.relative_path,
                        sha256=stored.sha256,
                        byte_size=stored.byte_size,
                        version=attempt_ordinal,
                        created_at=utc_now(),
                    )
            )
            item = self.persistence.create_interaction(
                    connection,
                    project_id=project.id,
                    conversation_id=conversation.id,
                    context_id=previous.context_id,
                    kind=InteractionKind.UNIT_REQUEST,
                    unit_id=previous.unit_id,
                    preparation_id=previous.preparation_id,
                    request_artifact_id=artifact.id,
                    request_sha256=digest,
                    transport_fingerprint=transport_fingerprint(strict_request_text(request)),
                    attempt=attempt,
                    supersedes_interaction_id=previous.id,
                    stage_run=run,
            )
        return self.run_interaction(item.id)

    def run_interaction(self, interaction_id: str) -> dict[str, object]:
        with self.database.connection() as connection:
            item = BrowserInteractionRepository(connection).get(interaction_id)
            conversation = BrowserConversationRepository(connection).get(item.conversation_id)
            project = ProjectRepository(connection).get(item.project_id)
        if item.status is InteractionStatus.IMPORTED:
            return self._payload(item)
        if item.status is InteractionStatus.BLOCKED:
            raise ConflictError(
                "BROWSER_INTERACTION_BLOCKED",
                "Blocked browser interaction requires explicit operator resolution",
                evidence={"interaction_id": item.id},
            )
        self._ensure_session_or_block(item)
        if not self.adapter.capture_gate_ready():
            self._block(
                item,
                "BROWSER_CAPTURE_SPIKE_REQUIRED",
                "Run and approve the short/long capture spike before real sends",
            )
        if item.status is InteractionStatus.CAPTURED:
            return self._import_captured(project.id, item)
        if item.status is InteractionStatus.SENT and item.kind is InteractionKind.UNIT_REQUEST:
            with self.database.connection() as connection:
                events = BrowserInteractionRepository(connection).events(item.id)
            local_proof = self._sent_local_successor_proof(events)
            if local_proof is not None:
                try:
                    return self._recover_sent_local_unit_request(
                        project.id,
                        item,
                        conversation,
                        events,
                        local_proof,
                    )
                except HeliosError as exc:
                    self._block(
                        item,
                        exc.code,
                        exc.message,
                        exc.context.evidence,
                    )
        sent_in_this_run = False
        if item.status is InteractionStatus.PREPARED:
            content = self._request_bytes(project.id, item)
            request_text = strict_request_text(content)
            if transport_fingerprint(request_text) != item.transport_fingerprint:
                self._block(
                    item,
                    "BROWSER_TRANSPORT_FINGERPRINT_MISMATCH",
                    "Request bytes no longer match their transport fingerprint",
                )
            if conversation.status is ConversationStatus.PROVISIONING:
                self.adapter.create_conversation()
                baseline = self.adapter.list_conversation_paths()
                with (
                    self.database.connection() as connection,
                    self.database.transaction(connection),
                ):
                    conversation = self.persistence.record_provisioning_baseline(
                        connection, conversation, baseline
                    )
            elif conversation.conversation_path is not None:
                self.adapter.open_conversation(conversation.conversation_path)
            def persist_send_attempt_started() -> None:
                nonlocal item
                anchor_reader = getattr(self.adapter, "pre_send_turn_anchor", None)
                anchor = anchor_reader() if callable(anchor_reader) else None
                if item.kind is InteractionKind.UNIT_REQUEST:
                    anchor = self._validated_unit_turn_anchor(
                        anchor,
                        expected_conversation_path=conversation.conversation_path,
                    )
                with (
                    self.database.connection() as connection,
                    self.database.transaction(connection),
                ):
                    current = BrowserInteractionRepository(connection).get(item.id)
                    item = self.persistence.begin_send(
                        connection,
                        current,
                        pre_send_turn_anchor=(
                            anchor
                            if item.kind is InteractionKind.UNIT_REQUEST
                            else None
                        ),
                    )

            try:
                self.adapter.send_message(
                    request_text,
                    on_send_attempt_started=persist_send_attempt_started,
                )
            except HeliosError as exc:
                self._persist_post_send_reconciliation_failure(item.id, exc)
                raise
            sent_in_this_run = True
            self._checkpoint("browser.after_send")
            with self.database.connection() as connection:
                item = BrowserInteractionRepository(connection).get(item.id)
            if item.status is not InteractionStatus.SENDING:
                raise IntegrityError(
                    "BROWSER_SEND_BOUNDARY_NOT_PERSISTED",
                    "Adapter returned without recording the send-attempt-started boundary",
                )
        if (
            item.status is InteractionStatus.SENDING
            and not sent_in_this_run
            and conversation.conversation_path is None
        ):
            resumed_path = self.adapter.current_conversation_path()
            if resumed_path is None:
                self._block(
                    item,
                    "BROWSER_SEND_REQUIRES_RECONCILE",
                    "A resumed send attempt has no recoverable canonical conversation path",
                )
            if not is_real_conversation_path(resumed_path):
                self._block(
                    item,
                    "BROWSER_CONVERSATION_PATH_INVALID",
                    "The resumed page URL is not a canonical /c/<uuid> path",
                )
        try:
            return self._observe_capture_import(
                project.id,
                item,
                conversation,
                structural_happy_path=sent_in_this_run,
            )
        except HeliosError as exc:
            self._persist_post_send_reconciliation_failure(item.id, exc)
            raise

    @staticmethod
    def _sent_local_successor_proof(
        events: list[dict[str, object]],
    ) -> dict[str, object] | None:
        for event in reversed(events):
            if event.get("status") != InteractionStatus.SENT.value:
                continue
            evidence = event.get("evidence")
            if (
                isinstance(evidence, dict)
                and evidence.get("proof_kind") == "post_send_local_successor_v1"
                and evidence.get("effect_boundary") == "send_observed"
            ):
                return evidence
        return None

    @classmethod
    def _blocked_local_unit_capture_recovery_proof(
        cls,
        item: BrowserInteraction,
        events: list[dict[str, object]],
        resolution: BrowserInteractionResolution | None,
    ) -> dict[str, object] | None:
        if (
            item.status is not InteractionStatus.BLOCKED
            or item.kind is not InteractionKind.UNIT_REQUEST
            or item.response_artifact_id is not None
            or item.response_sha256 is not None
            or item.imported_entity_id is not None
            or resolution is not None
        ):
            return None
        send_attempt_index: int | None = None
        for index, event in enumerate(events):
            evidence = event.get("evidence")
            if (
                isinstance(evidence, dict)
                and evidence.get("effect_boundary") == "send_attempt_started"
                and isinstance(evidence.get("pre_send_turn_anchor"), dict)
            ):
                send_attempt_index = index
        if send_attempt_index is None:
            return None

        for event in reversed(events[send_attempt_index + 1 :]):
            evidence = event.get("evidence")
            if not isinstance(evidence, dict) or (
                evidence.get("proof_kind") != "post_send_local_successor_v1"
            ):
                continue
            event_status = event.get("status")
            if event_status == InteractionStatus.SENT.value:
                if evidence.get("effect_boundary") != "send_observed":
                    continue
            elif event_status != InteractionStatus.BLOCKED.value:
                continue
            selected_user_id = evidence.get("selected_user_id")
            if isinstance(selected_user_id, str) and selected_user_id:
                return evidence
        return None

    @staticmethod
    def _authoritative_assistant_id(value: object) -> str | None:
        if (
            not isinstance(value, str)
            or not value
            or value.startswith(REQUEST_PLACEHOLDER_ASSISTANT_PREFIX)
        ):
            return None
        return value

    @classmethod
    def _local_successor_persistence_evidence(
        cls, evidence: dict[str, object]
    ) -> dict[str, object]:
        assistant_id = cls._authoritative_assistant_id(
            evidence.get("selected_assistant_id")
        )
        return {
            "proof_kind": evidence.get("proof_kind"),
            "selected_user_id": evidence.get("selected_user_id"),
            "selected_assistant_id": assistant_id,
            "assistant_identity_state": (
                "resolved" if assistant_id is not None else "unresolved"
            ),
        }

    def _persist_local_assistant_successor_latch(
        self,
        item: BrowserInteraction,
        conversation: BrowserConversation,
        *,
        selected_user_id: str,
        selected_assistant_id: str,
    ) -> None:
        with (
            self.database.connection() as connection,
            self.database.transaction(connection),
        ):
            interactions = BrowserInteractionRepository(connection)
            current = interactions.get(item.id)
            if (
                current.status not in {InteractionStatus.SENT, InteractionStatus.BLOCKED}
                or current.response_artifact_id is not None
                or current.response_sha256 is not None
                or current.imported_entity_id is not None
            ):
                raise ConflictError(
                    "BROWSER_RECOVERY_SOURCE_INVALID",
                    "Assistant successor latch source changed before persistence",
                )
            if (
                current.status is InteractionStatus.BLOCKED
                and BrowserInteractionResolutionRepository(connection).for_interaction(
                    current.id
                )
                is not None
            ):
                raise ConflictError(
                    "BROWSER_RECONCILE_INTERACTION_ABANDONED",
                    "An operator-abandoned interaction cannot persist assistant identity",
                )
            persisted_bindings = {
                (
                    str(evidence["selected_user_id"]),
                    str(evidence["selected_assistant_id"]),
                )
                for event in interactions.events(current.id)
                if isinstance((evidence := event.get("evidence")), dict)
                and evidence.get("event_type") == "assistant_successor_late_bound"
                and isinstance(evidence.get("selected_user_id"), str)
                and bool(evidence.get("selected_user_id"))
                and self._authoritative_assistant_id(
                    evidence.get("selected_assistant_id")
                )
                is not None
            }
            if persisted_bindings:
                if persisted_bindings != {(selected_user_id, selected_assistant_id)}:
                    raise ConflictError(
                        "BROWSER_RECOVERY_UNIT_LOCAL_BINDING_INVALID",
                        "Persisted assistant successor conflicts with the newly proven identity",
                    )
                return
            interactions.event(
                current.id,
                current.project_id,
                current.status,
                current.status,
                {
                    "event_type": "assistant_successor_late_bound",
                    "proof_kind": "post_send_local_successor_v1",
                    "identity_latched_before_capture": True,
                    "external_action_performed": False,
                    "conversation_path": conversation.conversation_path,
                    "selected_user_id": selected_user_id,
                    "selected_assistant_id": selected_assistant_id,
                    "assistant_identity_state": "resolved",
                },
            )

    def _recover_sent_local_unit_request(
        self,
        project_id: str,
        item: BrowserInteraction,
        conversation: BrowserConversation,
        events: list[dict[str, object]],
        local_proof: dict[str, object],
    ) -> dict[str, object]:
        if item.status not in {InteractionStatus.SENT, InteractionStatus.BLOCKED}:
            raise ConflictError(
                "BROWSER_RECOVERY_SOURCE_INVALID",
                "Local-successor capture recovery requires a sent or blocked interaction",
            )
        if item.status is InteractionStatus.BLOCKED:
            session = self.adapter.ensure_ready()
            if session is not SessionState.READY:
                raise ConflictError(
                    "BROWSER_RECONCILE_SESSION_NOT_READY",
                    "The dedicated browser profile is not ready for capture recovery",
                    evidence={"session_state": session.value},
                )
            if not self.adapter.capture_gate_ready():
                raise ConflictError(
                    "BROWSER_CAPTURE_SPIKE_REQUIRED",
                    "The rendered_text_v1 capture gate is not approved",
                )
        if item.response_artifact_id is not None or item.response_sha256 is not None:
            raise ConflictError(
                "BROWSER_RECOVERY_UNIT_RESPONSE_STATE_INVALID",
                "Sent unit recovery requires an interaction without captured response evidence",
            )
        selected_user_id = local_proof.get("selected_user_id")
        if not isinstance(selected_user_id, str) or not selected_user_id:
            raise ConflictError(
                "BROWSER_RECOVERY_UNIT_LOCAL_BINDING_INVALID",
                "Sent unit recovery requires the persisted local-successor user identity",
            )
        persisted_assistant_id = local_proof.get("selected_assistant_id")
        if persisted_assistant_id is not None and (
            not isinstance(persisted_assistant_id, str) or not persisted_assistant_id
        ):
            raise ConflictError(
                "BROWSER_RECOVERY_UNIT_LOCAL_BINDING_INVALID",
                "Sent unit recovery contains an invalid assistant identity",
            )
        selected_assistant_id = self._authoritative_assistant_id(
            persisted_assistant_id
        )
        turn_anchor, pre_send_user_turn_count, _send_attempt_started_at = (
            self._proven_unit_reconciliation_binding(item, conversation, events)
        )
        recovery_binding = {
            **turn_anchor,
            "recovery_selected_user_id": selected_user_id,
        }
        if selected_assistant_id is not None:
            recovery_binding["recovery_selected_assistant_id"] = selected_assistant_id
        if self.config.browser_capture_method_version != "rendered_text_v1":
            raise ConflictError(
                "BROWSER_RECOVERY_CAPTURE_METHOD_INVALID",
                "Sent unit local recovery requires rendered_text_v1",
            )
        conversation_path = conversation.conversation_path
        assert conversation_path is not None
        self.adapter.open_conversation(conversation_path)
        deadline = monotonic() + self.config.browser_timeout_seconds
        observations = 0
        last_evidence: dict[str, object] = {}
        while True:
            observations += 1
            inspection = self.adapter.inspect_reconciliation_unit_turn(
                recovery_binding,
                conversation_path,
            )
            last_evidence = inspection.evidence or {}
            if inspection.conversation_path not in {None, conversation_path}:
                raise ConflictError(
                    "BROWSER_RECOVERY_WRONG_CONVERSATION",
                    "Sent unit recovery moved away from its persisted conversation",
                )
            observed_user_id = last_evidence.get("selected_user_id")
            observed_assistant_id = self._authoritative_assistant_id(
                last_evidence.get("selected_assistant_id")
            )
            if observed_assistant_id is not None:
                if observed_user_id != selected_user_id:
                    raise ConflictError(
                        "BROWSER_RECOVERY_UNIT_LOCAL_BINDING_INVALID",
                        "Recovered local successor differs from the persisted user identity",
                        evidence={
                            "expected_user_id": selected_user_id,
                            "observed_user_id": observed_user_id,
                        },
                    )
                if (
                    selected_assistant_id is not None
                    and observed_assistant_id != selected_assistant_id
                ):
                    raise ConflictError(
                        "BROWSER_RECOVERY_UNIT_LOCAL_BINDING_INVALID",
                        "Recovered assistant successor differs from the persisted identity",
                        evidence={
                            "expected_assistant_id": selected_assistant_id,
                            "observed_assistant_id": observed_assistant_id,
                        },
                    )
                if selected_assistant_id is None:
                    self._persist_local_assistant_successor_latch(
                        item,
                        conversation,
                        selected_user_id=selected_user_id,
                        selected_assistant_id=observed_assistant_id,
                    )
                    self._checkpoint("browser.assistant_successor_latched")
                    selected_assistant_id = observed_assistant_id
                    recovery_binding["recovery_selected_assistant_id"] = (
                        observed_assistant_id
                    )
            if inspection.state is TurnState.AMBIGUOUS:
                raise ConflictError(
                    "BROWSER_RECOVERY_UNIT_TURN_AMBIGUOUS",
                    "Sent unit recovery found an ambiguous local successor",
                    evidence=last_evidence,
                )
            if inspection.state is TurnState.COMPLETE:
                if observed_assistant_id is None:
                    raise ConflictError(
                        "BROWSER_RECOVERY_UNIT_LOCAL_BINDING_INVALID",
                        "Recovered local successor has no authoritative assistant identity",
                    )
                break
            if monotonic() >= deadline:
                raise ConflictError(
                    "BROWSER_RECOVERY_UNIT_RESPONSE_INCOMPLETE",
                    "The locally bound unit response did not become complete before timeout",
                    evidence={
                        **last_evidence,
                        "pre_send_user_turn_count": pre_send_user_turn_count,
                        "observations": observations,
                    },
                )
            sleep(0.25)
        assert observed_assistant_id is not None
        captured = captured_response_bytes(
            self.adapter.capture_reconciled_unit_response(recovery_binding)
        )
        if not captured.strip():
            raise ConflictError(
                "BROWSER_RECOVERY_RESPONSE_EMPTY",
                "The locally bound unit response is empty",
            )
        stored = self.store.write_bytes(
            self._project_root(project_id),
            f"browser/responses/{item.id}/attempt-{item.attempt:04d}.txt",
            captured,
        )
        self._checkpoint("browser.response_stored")
        with (
            self.database.connection() as connection,
            self.database.transaction(connection),
        ):
            current = BrowserInteractionRepository(connection).get(item.id)
            if item.status is InteractionStatus.BLOCKED:
                if (
                    BrowserInteractionResolutionRepository(connection).for_interaction(
                        item.id
                    )
                    is not None
                ):
                    raise ConflictError(
                        "BROWSER_RECONCILE_INTERACTION_ABANDONED",
                        "An operator-abandoned interaction cannot resume capture recovery",
                    )
                current = self.persistence.resume_blocked_local_successor_capture(
                    connection,
                    current,
                    conversation,
                    conversation_path=conversation_path,
                    selected_user_id=selected_user_id,
                    selected_assistant_id=observed_assistant_id,
                    assistant_late_bound=selected_assistant_id is None,
                )
            elif current.status is not InteractionStatus.SENT:
                raise ConflictError(
                    "BROWSER_RECOVERY_SOURCE_INVALID",
                    "Sent unit recovery source changed while the response was observed",
                )
            project = ProjectRepository(connection).get(project_id)
            current = self.persistence.register_response(
                connection,
                project,
                current,
                stored,
                "rendered_text_v1",
            )
            current = self.persistence.transition(
                connection,
                current,
                InteractionStatus.CAPTURED,
            )
        self._checkpoint("browser.response_captured")
        return self._import_captured(project_id, current, captured)

    def reprobe_blocked_interaction(self, interaction_id: str) -> dict[str, object]:
        """Reinspect external evidence for one blocked interaction without resending."""
        with self.database.connection() as connection:
            item = BrowserInteractionRepository(connection).get(interaction_id)
            conversation = BrowserConversationRepository(connection).get(item.conversation_id)
        if item.status is not InteractionStatus.BLOCKED:
            raise ConflictError(
                "BROWSER_RECOVERY_SOURCE_INVALID",
                "Recovery reprobe requires a blocked browser interaction",
            )
        if conversation.conversation_path is None:
            return self._blocked_reprobe_result(
                item, "conversation_path_missing", observations=0
            )
        if not is_real_conversation_path(conversation.conversation_path):
            return {
                "outcome": "still_blocked",
                "interaction_id": item.id,
                "reason": "invalid_conversation_path",
                "observations": 0,
            }
        session = self.adapter.ensure_ready()
        if session is not SessionState.READY:
            return self._blocked_reprobe_result(
                item, f"session_{session.value}", observations=0
            )
        if not self.adapter.capture_gate_ready():
            return self._blocked_reprobe_result(
                item, "capture_gate_not_ready", observations=0
            )
        self.adapter.open_conversation(conversation.conversation_path)
        deadline = monotonic() + self.config.browser_timeout_seconds
        observations = 0
        while True:
            observations += 1
            inspection = self.adapter.inspect_turn(item.transport_fingerprint)
            if inspection.conversation_path not in {None, conversation.conversation_path}:
                return self._blocked_reprobe_result(
                    item, "wrong_conversation", observations=observations
                )
            if inspection.state is TurnState.AMBIGUOUS:
                return self._blocked_reprobe_result(
                    item, "turn_ambiguous", observations=observations
                )
            if inspection.state is TurnState.NOT_SENT:
                if monotonic() >= deadline:
                    return self._blocked_reprobe_result(
                        item, "user_turn_not_observed", observations=observations
                    )
                sleep(0.25)
                continue
            if inspection.observed_user_turn_fingerprint != item.transport_fingerprint:
                return self._blocked_reprobe_result(
                    item, "user_turn_fingerprint_not_observed", observations=observations
                )
            with (
                self.database.connection() as connection,
                self.database.transaction(connection),
            ):
                current = BrowserInteractionRepository(connection).get(item.id)
                item = self.persistence.resume_blocked_from_proven_user_turn(
                    connection,
                    current,
                    conversation_path=conversation.conversation_path,
                    observed_user_turn_fingerprint=(
                        inspection.observed_user_turn_fingerprint
                    ),
                    evidence={
                        "conversation_path": conversation.conversation_path,
                        "turn_state": inspection.state.value,
                    },
                )
            result = self._observe_capture_import(item.project_id, item, conversation)
            return {
                "outcome": "recovered",
                "interaction_id": item.id,
                "observations": observations,
                "result": result,
            }

    def reconcile_blocked_interaction(
        self, project_id: str, interaction_id: str
    ) -> dict[str, object]:
        """Observe and reconcile one proven external send without sending anything."""
        self._assert_browser_enabled(project_id)
        with self.database.connection() as connection:
            ProjectRepository(connection).get(project_id)
            interactions = BrowserInteractionRepository(connection)
            item = interactions.get(interaction_id)
            if item.project_id != project_id:
                raise NotFoundError(
                    "BROWSER_INTERACTION_NOT_FOUND",
                    "Browser interaction was not found in the requested project",
                )
            conversation = BrowserConversationRepository(connection).get(item.conversation_id)
            resolution = BrowserInteractionResolutionRepository(connection).for_interaction(
                item.id
            )
            events = interactions.events(item.id)
        if item.status is not InteractionStatus.BLOCKED:
            raise ConflictError(
                "BROWSER_RECONCILE_SOURCE_INVALID",
                "Explicit reconciliation requires a blocked interaction",
            )
        if resolution is not None:
            raise ConflictError(
                "BROWSER_RECONCILE_INTERACTION_ABANDONED",
                "An operator-abandoned interaction cannot be reconciled",
            )
        if (
            item.response_artifact_id is not None
            or item.response_sha256 is not None
            or item.imported_entity_id is not None
        ):
            raise ConflictError(
                "BROWSER_RECONCILE_SOURCE_DIVERGENT",
                "Blocked reconciliation source already contains downstream evidence",
            )
        local_capture_proof = self._blocked_local_unit_capture_recovery_proof(
            item, events, resolution
        )
        if local_capture_proof is not None:
            result = self._recover_sent_local_unit_request(
                project_id,
                item,
                conversation,
                events,
                local_capture_proof,
            )
            return {
                "outcome": "reconciled",
                "interaction_id": item.id,
                "conversation_path": conversation.conversation_path,
                "proof_kind": "post_send_local_successor_v1",
                "result": result,
            }
        if self._last_blocking_error_code(events) not in EXPLICIT_RECONCILIATION_ERROR_CODES:
            raise ConflictError(
                "BROWSER_RECONCILE_SOURCE_INVALID",
                "Only an unprovable post-Send interaction or a locally proven "
                "unit capture recovery is eligible for reconciliation",
            )
        if item.kind is InteractionKind.UNIT_REQUEST:
            return self._reconcile_blocked_unit_request(
                project_id, item, conversation, events
            )
        if item.kind is not InteractionKind.CONTEXT_LOAD:
            raise ConflictError(
                "BROWSER_RECONCILE_KIND_UNSUPPORTED",
                "Explicit reconciliation supports context loads and unit requests",
            )
        send_attempt_started_at = self._send_attempt_started_at(events)
        baseline = self._reconciliation_baseline(conversation.provisioning_baseline_json)
        if self.config.browser_capture_method_version != "rendered_text_v1":
            raise ConflictError(
                "BROWSER_RECONCILE_CAPTURE_METHOD_INVALID",
                "Explicit reconciliation requires rendered_text_v1",
            )
        session = self.adapter.ensure_ready()
        if session is not SessionState.READY:
            raise ConflictError(
                "BROWSER_RECONCILE_SESSION_NOT_READY",
                "The dedicated browser profile is not ready for reconciliation",
                evidence={"session_state": session.value},
            )
        if not self.adapter.capture_gate_ready():
            raise ConflictError(
                "BROWSER_CAPTURE_SPIKE_REQUIRED",
                "The rendered_text_v1 capture gate is not approved",
            )

        candidate_path: str | None = None
        candidate_observed_at: str | None = None
        current_path = self.adapter.current_conversation_path()
        if (
            current_path is not None
            and is_real_conversation_path(current_path)
            and current_path not in baseline
        ):
            inspection = self.adapter.inspect_reconciliation_turn(
                item.transport_fingerprint
            )
            if (
                inspection.observed_user_turn_fingerprint
                == item.transport_fingerprint
            ):
                candidate_path = current_path

        if candidate_path is None:
            candidates = tuple(
                candidate
                for candidate in self.adapter.find_reconciliation_candidates(
                    item.transport_fingerprint, send_attempt_started_at
                )
                if is_real_conversation_path(candidate.conversation_path)
                and candidate.conversation_path not in baseline
                and candidate.user_turn_fingerprint == item.transport_fingerprint
                and self._timestamp_at_or_after(
                    candidate.observed_at, send_attempt_started_at
                )
            )
            distinct = {candidate.conversation_path: candidate for candidate in candidates}
            if not distinct:
                raise ConflictError(
                    "BROWSER_RECONCILE_CANDIDATE_NOT_FOUND",
                    "No recent conversation proved the complete prepared request fingerprint",
                    evidence={"candidate_count": 0},
                )
            if len(distinct) != 1:
                raise ConflictError(
                    "BROWSER_RECONCILE_CANDIDATE_AMBIGUOUS",
                    "More than one recent conversation proved the prepared request fingerprint",
                    evidence={"candidate_count": len(distinct)},
                )
            candidate = next(iter(distinct.values()))
            candidate_path = candidate.conversation_path
            candidate_observed_at = candidate.observed_at

        if not is_real_conversation_path(candidate_path):
            raise ConflictError(
                "BROWSER_CONVERSATION_PATH_INVALID",
                "Reconciliation candidate is not a canonical /c/<uuid> path",
            )
        self.adapter.open_conversation(candidate_path)
        deadline = monotonic() + self.config.browser_timeout_seconds
        observations = 0
        while True:
            observations += 1
            inspection = self.adapter.inspect_reconciliation_turn(
                item.transport_fingerprint
            )
            if inspection.conversation_path not in {None, candidate_path}:
                raise ConflictError(
                    "BROWSER_RECONCILE_WRONG_CONVERSATION",
                    "Reconciliation observation moved to another conversation",
                )
            if (
                inspection.observed_user_turn_fingerprint
                != item.transport_fingerprint
            ):
                raise ConflictError(
                    "BROWSER_RECONCILE_FINGERPRINT_MISMATCH",
                    "Candidate first user turn does not match the prepared request",
                )
            if inspection.state is TurnState.COMPLETE:
                break
            if inspection.state is TurnState.AMBIGUOUS:
                raise ConflictError(
                    "BROWSER_RECONCILE_RESPONSE_AMBIGUOUS",
                    "The corresponding assistant response is structurally ambiguous",
                )
            if monotonic() >= deadline:
                raise ConflictError(
                    "BROWSER_RECONCILE_RESPONSE_INCOMPLETE",
                    "The corresponding assistant response did not become complete",
                    evidence={"observation_count": observations},
                )
            sleep(0.25)

        captured = captured_response_bytes(
            self.adapter.capture_reconciled_response(item.transport_fingerprint)
        )
        if not captured.strip():
            raise ConflictError(
                "BROWSER_RECONCILE_RESPONSE_EMPTY",
                "The reconciled rendered response is empty",
            )
        stored = self.store.write_bytes(
            self._project_root(project_id),
            f"browser/responses/{item.id}/attempt-{item.attempt:04d}.txt",
            captured,
        )
        sent_at = candidate_observed_at or send_attempt_started_at
        with (
            self.database.connection() as connection,
            self.database.transaction(connection),
        ):
            current = BrowserInteractionRepository(connection).get(item.id)
            if (
                BrowserInteractionResolutionRepository(connection).for_interaction(item.id)
                is not None
            ):
                raise ConflictError(
                    "BROWSER_RECONCILE_INTERACTION_ABANDONED",
                    "Interaction was abandoned while reconciliation was observing evidence",
                )
            current_conversation = BrowserConversationRepository(connection).get(
                conversation.id
            )
            item, conversation = (
                self.persistence.reconcile_blocked_user_turn_and_bind_conversation(
                    connection,
                    current,
                    current_conversation,
                    conversation_path=candidate_path,
                    observed_user_turn_fingerprint=item.transport_fingerprint,
                    sent_at=sent_at,
                )
            )
            project = ProjectRepository(connection).get(project_id)
            item = self.persistence.register_response(
                connection,
                project,
                item,
                stored,
                "rendered_text_v1",
            )
            item = self.persistence.transition(
                connection, item, InteractionStatus.CAPTURED
            )
        result = self._import_captured(project_id, item, captured)
        LOGGER.info(
            "browser_reconcile interaction_id=%s conversation_path=%s "
            "transport_fingerprint=%s response_sha256=%s response_artifact_id=%s",
            item.id,
            candidate_path,
            item.transport_fingerprint,
            sha256_bytes(captured),
            item.response_artifact_id,
            extra={"operation": "browser.reconcile", "status": "reconciled"},
        )
        return {
            "outcome": "reconciled",
            "interaction_id": item.id,
            "conversation_path": candidate_path,
            "transport_fingerprint": item.transport_fingerprint,
            "response_sha256": sha256_bytes(captured),
            "observations": observations,
            "result": result,
        }

    def _reconcile_blocked_unit_request(
        self,
        project_id: str,
        item: BrowserInteraction,
        conversation: BrowserConversation,
        events: list[dict[str, object]],
    ) -> dict[str, object]:
        turn_anchor, pre_send_user_turn_count, send_attempt_started_at = (
            self._proven_unit_reconciliation_binding(item, conversation, events)
        )
        if self.config.browser_capture_method_version != "rendered_text_v1":
            raise ConflictError(
                "BROWSER_RECONCILE_CAPTURE_METHOD_INVALID",
                "Explicit unit reconciliation requires rendered_text_v1",
            )
        session = self.adapter.ensure_ready()
        if session is not SessionState.READY:
            raise ConflictError(
                "BROWSER_RECONCILE_SESSION_NOT_READY",
                "The dedicated browser profile is not ready for unit reconciliation",
                evidence={"session_state": session.value},
            )
        if not self.adapter.capture_gate_ready():
            raise ConflictError(
                "BROWSER_CAPTURE_SPIKE_REQUIRED",
                "The rendered_text_v1 capture gate is not approved",
            )
        conversation_path = conversation.conversation_path
        assert conversation_path is not None
        self.adapter.open_conversation(conversation_path)
        observation_started_at = monotonic()
        deadline = observation_started_at + self.config.browser_timeout_seconds
        observations = 0
        last_evidence: dict[str, object] = {}
        hydration_signature: tuple[int, int, int] | None = None
        hydration_signature_started_at: float | None = None
        hydration_signature_reads = 0
        hydration_reload_count = 0
        pre_reload_counts: dict[str, int] | None = None
        post_reload_counts: dict[str, int] | None = None
        while True:
            observations += 1
            inspection = self.adapter.inspect_reconciliation_unit_turn(
                turn_anchor,
                conversation_path,
            )
            last_evidence = inspection.evidence or {}
            user_count = self._evidence_count(last_evidence, "user_count")
            assistant_count = self._evidence_count(last_evidence, "assistant_count")
            turn_count = self._evidence_count(last_evidence, "turn_count")
            observed_counts = {
                "user_count": user_count,
                "assistant_count": assistant_count,
                "turn_count": turn_count,
            }
            if hydration_reload_count:
                post_reload_counts = observed_counts
            if inspection.conversation_path not in {None, conversation_path}:
                raise ConflictError(
                    "BROWSER_RECONCILE_WRONG_CONVERSATION",
                    "Unit reconciliation observation moved to another conversation",
                )
            if inspection.state is TurnState.COMPLETE:
                break
            if inspection.state is TurnState.AMBIGUOUS:
                raise ConflictError(
                    "BROWSER_RECONCILE_UNIT_TURN_AMBIGUOUS",
                    "Unit reconciliation found unexplained user or assistant turns",
                    evidence={
                        **last_evidence,
                        **self._hydration_reload_evidence(
                            hydration_reload_count,
                            pre_reload_counts,
                            post_reload_counts,
                        ),
                    },
                )
            now = monotonic()
            failure_reason = last_evidence.get("failure_reason")
            if (
                failure_reason == "turn_cardinality_hydrating"
                and any(observed_counts.values())
            ):
                signature = (user_count, assistant_count, turn_count)
                if signature == hydration_signature:
                    hydration_signature_reads += 1
                else:
                    hydration_signature = signature
                    hydration_signature_started_at = now
                    hydration_signature_reads = 1
                stalled_for = (
                    0.0
                    if hydration_signature_started_at is None
                    else now - hydration_signature_started_at
                )
                if (
                    hydration_reload_count == 0
                    and hydration_signature_reads >= 2
                    and now < deadline
                    and stalled_for
                    >= UNIT_RECONCILIATION_HYDRATION_STALL_SECONDS
                ):
                    pre_reload_counts = observed_counts
                    hydration_reload_count = 1
                    LOGGER.info(
                        "browser_reconcile_unit_hydration "
                        "hydration_reload_triggered=%s hydration_reload_count=%s "
                        "pre_reload_counts=%s post_reload_counts=%s",
                        True,
                        hydration_reload_count,
                        pre_reload_counts,
                        post_reload_counts,
                        extra={
                            "operation": "browser.reconcile.unit_hydration",
                            "status": "reload_triggered",
                        },
                    )
                    self.adapter.reload_conversation(conversation_path)
                    hydration_signature = None
                    hydration_signature_started_at = None
                    hydration_signature_reads = 0
                    continue
            else:
                hydration_signature = None
                hydration_signature_started_at = None
                hydration_signature_reads = 0
            if now >= deadline:
                incomplete_evidence = {
                    "ordinal": pre_send_user_turn_count,
                    "user_count": last_evidence.get("user_count", 0),
                    "assistant_count": last_evidence.get("assistant_count", 0),
                    "selected_user_index": last_evidence.get("selected_user_index"),
                    "selected_assistant_index": last_evidence.get(
                        "selected_assistant_index"
                    ),
                    "selected_user_id": last_evidence.get("selected_user_id"),
                    "selected_assistant_id": last_evidence.get(
                        "selected_assistant_id"
                    ),
                    "expected_user_count": last_evidence.get(
                        "expected_user_count", pre_send_user_turn_count + 1
                    ),
                    "turn_count": last_evidence.get("turn_count", 0),
                    "turn_message_id_count": last_evidence.get(
                        "turn_message_id_count", 0
                    ),
                    "structure_stable_count": last_evidence.get(
                        "structure_stable_count", 0
                    ),
                    "semantic_container_count": last_evidence.get(
                        "semantic_container_count", 0
                    ),
                    "semantic_visible_count": last_evidence.get(
                        "semantic_visible_count", 0
                    ),
                    "semantic_length": last_evidence.get("semantic_length", 0),
                    "semantic_sha_same": last_evidence.get(
                        "semantic_sha_same", False
                    ),
                    "no_generation_indicator": last_evidence.get(
                        "no_generation_indicator", True
                    ),
                    "no_stop": last_evidence.get("no_stop", True),
                    "post_response_control_count": last_evidence.get(
                        "post_response_control_count", 0
                    ),
                    "stable_read_count": last_evidence.get("stable_read_count", 0),
                    "elapsed_ms": int((now - observation_started_at) * 1000),
                    "failure_reason": last_evidence.get(
                        "failure_reason", "completion_observation_missing"
                    ),
                    **self._hydration_reload_evidence(
                        hydration_reload_count,
                        pre_reload_counts,
                        post_reload_counts,
                    ),
                }
                LOGGER.error(
                    "browser_reconcile_unit_incomplete ordinal=%s user_count=%s "
                    "assistant_count=%s selected_user_index=%s "
                    "selected_assistant_index=%s expected_user_count=%s "
                    "turn_count=%s turn_message_id_count=%s "
                    "structure_stable_count=%s semantic_container_count=%s "
                    "semantic_visible_count=%s semantic_length=%s "
                    "semantic_sha_same=%s no_generation_indicator=%s no_stop=%s "
                    "post_response_control_count=%s stable_read_count=%s "
                    "elapsed_ms=%s failure_reason=%s",
                    incomplete_evidence["ordinal"],
                    incomplete_evidence["user_count"],
                    incomplete_evidence["assistant_count"],
                    incomplete_evidence["selected_user_index"],
                    incomplete_evidence["selected_assistant_index"],
                    incomplete_evidence["expected_user_count"],
                    incomplete_evidence["turn_count"],
                    incomplete_evidence["turn_message_id_count"],
                    incomplete_evidence["structure_stable_count"],
                    incomplete_evidence["semantic_container_count"],
                    incomplete_evidence["semantic_visible_count"],
                    incomplete_evidence["semantic_length"],
                    incomplete_evidence["semantic_sha_same"],
                    incomplete_evidence["no_generation_indicator"],
                    incomplete_evidence["no_stop"],
                    incomplete_evidence["post_response_control_count"],
                    incomplete_evidence["stable_read_count"],
                    incomplete_evidence["elapsed_ms"],
                    incomplete_evidence["failure_reason"],
                    extra={
                        "operation": "browser.reconcile.unit_completion",
                        "status": "incomplete",
                    },
                )
                hydration_reasons = {
                    "conversation_path_not_loaded",
                    "conversation_root_not_hydrated",
                    "turn_cardinality_hydrating",
                    "turn_anchor_missing",
                    "turn_successor_user_missing",
                    "structure_not_stable",
                }
                failure_reason = incomplete_evidence["failure_reason"]
                if failure_reason in hydration_reasons:
                    raise ConflictError(
                        "BROWSER_RECONCILE_UNIT_TURN_HYDRATION_TIMEOUT",
                        "The ordinal unit turn cardinality did not hydrate and stabilize",
                        evidence=incomplete_evidence,
                    )
                raise ConflictError(
                    "BROWSER_RECONCILE_UNIT_RESPONSE_INCOMPLETE",
                    "The ordinal unit response did not become structurally complete",
                    evidence=incomplete_evidence,
                )
            sleep(0.25)
        hydration_evidence = self._hydration_reload_evidence(
            hydration_reload_count,
            pre_reload_counts,
            post_reload_counts,
        )
        LOGGER.info(
            "browser_reconcile_unit_hydration hydration_reload_triggered=%s "
            "hydration_reload_count=%s pre_reload_counts=%s post_reload_counts=%s",
            hydration_evidence["hydration_reload_triggered"],
            hydration_evidence["hydration_reload_count"],
            hydration_evidence["pre_reload_counts"],
            hydration_evidence["post_reload_counts"],
            extra={
                "operation": "browser.reconcile.unit_hydration",
                "status": "stabilized",
            },
        )
        captured = captured_response_bytes(
            self.adapter.capture_reconciled_unit_response(turn_anchor)
        )
        if not captured.strip():
            raise ConflictError(
                "BROWSER_RECONCILE_RESPONSE_EMPTY",
                "The reconciled rendered unit response is empty",
            )
        stored = self.store.write_bytes(
            self._project_root(project_id),
            f"browser/responses/{item.id}/attempt-{item.attempt:04d}.txt",
            captured,
        )
        with (
            self.database.connection() as connection,
            self.database.transaction(connection),
        ):
            current = BrowserInteractionRepository(connection).get(item.id)
            if (
                BrowserInteractionResolutionRepository(connection).for_interaction(item.id)
                is not None
            ):
                raise ConflictError(
                    "BROWSER_RECONCILE_INTERACTION_ABANDONED",
                    "Unit interaction was abandoned while reconciliation observed evidence",
                )
            current_conversation = BrowserConversationRepository(connection).get(
                conversation.id
            )
            item = self.persistence.reconcile_blocked_unit_turn(
                connection,
                current,
                current_conversation,
                conversation_path=conversation_path,
                user_turn_ordinal=pre_send_user_turn_count,
                sent_at=send_attempt_started_at,
                selected_user_id=(
                    str(last_evidence["selected_user_id"])
                    if isinstance(last_evidence.get("selected_user_id"), str)
                    else None
                ),
                selected_assistant_id=(
                    str(last_evidence["selected_assistant_id"])
                    if isinstance(last_evidence.get("selected_assistant_id"), str)
                    else None
                ),
            )
            project = ProjectRepository(connection).get(project_id)
            item = self.persistence.register_response(
                connection,
                project,
                item,
                stored,
                "rendered_text_v1",
            )
            item = self.persistence.transition(
                connection, item, InteractionStatus.CAPTURED
            )
        result = self._import_captured(project_id, item, captured)
        LOGGER.info(
            "browser_reconcile_unit interaction_id=%s conversation_path=%s "
            "user_turn_ordinal=%s transport_fingerprint=%s response_sha256=%s "
            "response_artifact_id=%s preparation_id=%s unit_id=%s",
            item.id,
            conversation_path,
            pre_send_user_turn_count,
            item.transport_fingerprint,
            sha256_bytes(captured),
            item.response_artifact_id,
            item.preparation_id,
            item.unit_id,
            extra={"operation": "browser.reconcile.unit", "status": "reconciled"},
        )
        return {
            "outcome": "reconciled",
            "interaction_id": item.id,
            "conversation_path": conversation_path,
            "transport_fingerprint": item.transport_fingerprint,
            "response_sha256": sha256_bytes(captured),
            "user_turn_ordinal": pre_send_user_turn_count,
            "observations": observations,
            **hydration_evidence,
            "result": result,
        }

    @staticmethod
    def _evidence_count(evidence: dict[str, object], key: str) -> int:
        value = evidence.get(key)
        return value if isinstance(value, int) and value >= 0 else 0

    @staticmethod
    def _hydration_reload_evidence(
        reload_count: int,
        pre_reload_counts: dict[str, int] | None,
        post_reload_counts: dict[str, int] | None,
    ) -> dict[str, object]:
        return {
            "hydration_reload_triggered": reload_count > 0,
            "hydration_reload_count": reload_count,
            "pre_reload_counts": pre_reload_counts,
            "post_reload_counts": post_reload_counts,
        }

    def _proven_unit_reconciliation_binding(
        self,
        item: BrowserInteraction,
        conversation: BrowserConversation,
        events: list[dict[str, object]],
    ) -> tuple[dict[str, object], int, str]:
        if (
            item.kind is not InteractionKind.UNIT_REQUEST
            or item.preparation_id is None
            or item.unit_id is None
        ):
            raise ConflictError(
                "BROWSER_RECONCILE_UNIT_BINDING_INVALID",
                "Unit reconciliation requires persisted unit and preparation identities",
            )
        if (
            conversation.status is not ConversationStatus.READY
            or conversation.context_id != item.context_id
            or conversation.conversation_path is None
            or not is_real_conversation_path(conversation.conversation_path)
            or conversation.first_turn_fingerprint is None
        ):
            raise ConflictError(
                "BROWSER_RECONCILE_UNIT_CONVERSATION_INVALID",
                "Unit reconciliation requires the context's proven ready conversation",
            )
        send_attempt_started_at = self._send_attempt_started_at(events)
        with self.database.connection() as connection:
            preparation = PreparationRepository(connection).get(item.preparation_id)
            if (
                preparation.project_id != item.project_id
                or preparation.context_id != item.context_id
                or preparation.unit_id != item.unit_id
                or preparation.request_artifact_id != item.request_artifact_id
                or preparation.request_sha256 != item.request_sha256
            ):
                raise ConflictError(
                    "BROWSER_RECONCILE_UNIT_PREPARATION_MISMATCH",
                    "Persisted interaction does not match its exact unit preparation",
                )
        persisted_anchor: object = None
        for event in reversed(events):
            evidence = event.get("evidence")
            if (
                isinstance(evidence, dict)
                and evidence.get("effect_boundary") == "send_attempt_started"
            ):
                persisted_anchor = evidence.get("pre_send_turn_anchor")
                break
        turn_anchor = self._validated_unit_turn_anchor(
            persisted_anchor,
            expected_conversation_path=conversation.conversation_path,
            error_code="BROWSER_RECONCILE_UNIT_ANCHOR_MISSING",
        )
        pre_send_count = turn_anchor["pre_send_user_turn_count"]
        assert isinstance(pre_send_count, int)
        return turn_anchor, pre_send_count, send_attempt_started_at

    def block_orphaned_sending_interaction(
        self, project_id: str, interaction_id: str
    ) -> dict[str, object]:
        """Promote one proven orphaned sending interaction to blocked without browser I/O."""
        error_code = "BROWSER_SEND_REQUIRES_RECONCILE"
        with (
            self.database.connection() as connection,
            self.database.transaction(connection),
        ):
            ProjectRepository(connection).get(project_id)
            interactions = BrowserInteractionRepository(connection)
            item = interactions.get(interaction_id)
            if item.project_id != project_id:
                raise NotFoundError(
                    "BROWSER_INTERACTION_NOT_FOUND",
                    "Browser interaction was not found in the requested project",
                )
            run = StageRunRepository(connection).get(item.stage_run_id)
            events = interactions.events(item.id)
            if item.status is InteractionStatus.BLOCKED:
                if (
                    self._last_blocking_error_code(events) == error_code
                    and run.status is RunStatus.BLOCKED
                    and run.finished_at is not None
                ):
                    return {
                        "outcome": "already_blocked",
                        "interaction_id": item.id,
                        "status": item.status.value,
                        "error_code": error_code,
                    }
                raise ConflictError(
                    "BROWSER_ORPHANED_SEND_SOURCE_DIVERGENT",
                    "Blocked interaction does not match the orphaned-send resolution",
                )
            if item.status is not InteractionStatus.SENDING:
                raise ConflictError(
                    "BROWSER_ORPHANED_SEND_SOURCE_INVALID",
                    "Orphaned-send resolution requires a sending interaction",
                    evidence={"interaction_id": item.id, "status": item.status.value},
                )
            if not self._has_send_attempt_started_event(events):
                raise ConflictError(
                    "BROWSER_ORPHANED_SEND_BOUNDARY_MISSING",
                    "Sending interaction has no send-attempt-started event",
                )
            terminal_statuses = {
                InteractionStatus.SENT.value,
                InteractionStatus.CAPTURED.value,
                InteractionStatus.IMPORTED.value,
            }
            if any(event.get("status") in terminal_statuses for event in events):
                raise ConflictError(
                    "BROWSER_ORPHANED_SEND_DOWNSTREAM_EVIDENCE",
                    "Orphaned sending interaction already has a downstream status event",
                )
            if (
                item.sent_at is not None
                or item.response_artifact_id is not None
                or item.response_sha256 is not None
                or item.capture_method_version is not None
                or item.captured_at is not None
                or item.imported_entity_type is not None
                or item.imported_entity_id is not None
                or item.imported_at is not None
            ):
                raise ConflictError(
                    "BROWSER_ORPHANED_SEND_DOWNSTREAM_EVIDENCE",
                    "Orphaned sending interaction already has downstream evidence",
                )
            if run.status is not RunStatus.RUNNING or run.finished_at is not None:
                raise ConflictError(
                    "BROWSER_ORPHANED_SEND_STAGE_RUN_INVALID",
                    "Orphaned-send resolution requires a still-running StageRun",
                    evidence={
                        "stage_run_id": run.id,
                        "stage_run_status": run.status.value,
                        "stage_run_finished": run.finished_at is not None,
                    },
                )
            resolution = BrowserInteractionResolutionRepository(connection).for_interaction(
                item.id
            )
            if resolution is not None:
                raise ConflictError(
                    "BROWSER_ORPHANED_SEND_ALREADY_RESOLVED",
                    "Orphaned sending interaction already has an operator resolution",
                )
            blocked = self.persistence.transition(
                connection,
                item,
                InteractionStatus.BLOCKED,
                evidence={
                    "error_code": error_code,
                    "resolution": "orphaned_sending_to_blocked",
                },
            )
            ErrorRepository(connection).add(
                project_id=blocked.project_id,
                stage_run_id=blocked.stage_run_id,
                code=error_code,
                message="Orphaned post-Send interaction requires explicit reconciliation",
                recoverable=False,
                evidence={"interaction_id": blocked.id},
            )
            finished_run = StageRunRepository(connection).get(blocked.stage_run_id)
            if finished_run.status is not RunStatus.BLOCKED or finished_run.finished_at is None:
                raise IntegrityError(
                    "BROWSER_ORPHANED_SEND_BLOCK_INCOMPLETE",
                    "Orphaned-send resolution did not finish the blocked StageRun",
                )
        return {
            "outcome": "blocked",
            "interaction_id": blocked.id,
            "status": blocked.status.value,
            "error_code": error_code,
        }

    def resume_captured_import(
        self, project_id: str, interaction_id: str
    ) -> dict[str, object]:
        """Import one already-captured unit response without any browser operation."""
        with self.database.connection() as connection:
            project = ProjectRepository(connection).get(project_id)
            item = BrowserInteractionRepository(connection).get(interaction_id)
            if item.project_id != project.id:
                raise NotFoundError(
                    "BROWSER_INTERACTION_NOT_FOUND",
                    "Browser interaction was not found in the requested project",
                )
            if item.status not in {
                InteractionStatus.CAPTURED,
                InteractionStatus.IMPORTED,
            }:
                raise ConflictError(
                    "BROWSER_IMPORT_RESUME_SOURCE_INVALID",
                    "Import resume requires a captured interaction",
                    evidence={"interaction_id": item.id, "status": item.status.value},
                )
            if item.kind is not InteractionKind.UNIT_REQUEST:
                raise ConflictError(
                    "BROWSER_IMPORT_RESUME_KIND_UNSUPPORTED",
                    "Import resume currently requires a unit request",
                )
            if (
                item.preparation_id is None
                or item.unit_id is None
                or item.response_artifact_id is None
                or item.response_sha256 is None
                or item.capture_method_version is None
            ):
                raise IntegrityError(
                    "BROWSER_IMPORT_RESUME_BINDING_INVALID",
                    "Captured unit interaction is missing persisted import evidence",
                )
            preparation = PreparationRepository(connection).get(item.preparation_id)
            preparation_run = StageRunRepository(connection).get(
                preparation.stage_run_id
            )
            if (
                preparation.project_id != project.id
                or preparation.context_id != item.context_id
                or preparation.unit_id != item.unit_id
                or preparation.request_artifact_id != item.request_artifact_id
                or preparation.request_sha256 != item.request_sha256
                or preparation_run.status is not RunStatus.DONE
            ):
                raise IntegrityError(
                    "BROWSER_IMPORT_RESUME_BINDING_INVALID",
                    "Interaction does not match its exact completed unit preparation",
                )
            artifact = next(
                (
                    candidate
                    for candidate in ArtifactRepository(connection).list_for_project(project.id)
                    if candidate.id == item.response_artifact_id
                ),
                None,
            )
            if (
                artifact is None
                or artifact.stage_run_id != item.stage_run_id
                or artifact.artifact_type != "browser_response_raw"
                or artifact.sha256 != item.response_sha256
            ):
                raise IntegrityError(
                    "BROWSER_RESPONSE_ARTIFACT_INVALID",
                    "Response artifact owner or binding differs",
                )
            run = StageRunRepository(connection).get(item.stage_run_id)
            expected_run_status = (
                RunStatus.RUNNING
                if item.status is InteractionStatus.CAPTURED
                else RunStatus.DONE
            )
            if run.status is not expected_run_status:
                raise IntegrityError(
                    "BROWSER_STAGE_RUN_DIVERGENT",
                    "Browser interaction and StageRun disagree at import resume",
                )
        response = self._response_bytes(project_id, item)
        if item.status is InteractionStatus.CAPTURED:
            if (
                item.imported_entity_type is not None
                or item.imported_entity_id is not None
                or item.imported_at is not None
            ):
                raise IntegrityError(
                    "BROWSER_IMPORT_RESUME_BINDING_INVALID",
                    "Captured interaction already contains partial import identity",
                )
            result = self._import_captured(project_id, item, response)
            outcome = "imported"
        else:
            if (
                item.imported_entity_type != "text_unit_submission"
                or item.imported_entity_id is None
                or item.imported_at is None
            ):
                raise IntegrityError(
                    "BROWSER_IMPORT_IDENTITY_DIVERGENT",
                    "Imported interaction lacks its exact M2 submission identity",
                )
            with self.database.connection() as connection:
                submission = SubmissionRepository(connection).get(item.imported_entity_id)
            if (
                submission.project_id != project_id
                or submission.context_id != item.context_id
                or submission.preparation_id != item.preparation_id
                or submission.unit_id != item.unit_id
                or submission.raw_sha256 != item.response_sha256
                or submission.raw_artifact_id is None
                or submission.validation_report_artifact_id is None
            ):
                raise IntegrityError(
                    "BROWSER_IMPORT_IDENTITY_DIVERGENT",
                    "Persisted M2 submission differs from the captured interaction",
                )
            result = self._payload(item)
            result["disposition"] = submission.disposition.value
            result["paused"] = (
                submission.disposition is not SubmissionDisposition.ACCEPTED
            )
            outcome = "already_imported"
        LOGGER.info(
            "browser_resume_import interaction_id=%s response_artifact_id=%s "
            "response_sha256=%s preparation_id=%s imported_entity_id=%s outcome=%s",
            item.id,
            item.response_artifact_id,
            item.response_sha256,
            item.preparation_id,
            result.get("imported_entity_id"),
            outcome,
            extra={"operation": "browser.interaction.resume_import", "status": outcome},
        )
        return {**result, "outcome": outcome}

    def recapture_unit_response(
        self, project_id: str, interaction_id: str
    ) -> dict[str, object]:
        """Recapture one persisted ordinal unit response without any external write."""
        self._assert_browser_enabled(project_id)
        with self.database.connection() as connection:
            project = ProjectRepository(connection).get(project_id)
            interactions = BrowserInteractionRepository(connection)
            item = interactions.get(interaction_id)
            if item.project_id != project.id:
                raise NotFoundError(
                    "BROWSER_INTERACTION_NOT_FOUND",
                    "Browser interaction was not found in the requested project",
                )
            if item.status not in {
                InteractionStatus.CAPTURED,
                InteractionStatus.IMPORTED,
            }:
                raise ConflictError(
                    "BROWSER_RECAPTURE_SOURCE_INVALID",
                    "Response recapture requires a captured or imported interaction",
                    evidence={"interaction_id": item.id, "status": item.status.value},
                )
            if (
                item.kind is not InteractionKind.UNIT_REQUEST
                or item.preparation_id is None
                or item.unit_id is None
                or item.response_artifact_id is None
                or item.response_sha256 is None
                or item.capture_method_version != "rendered_text_v1"
            ):
                raise IntegrityError(
                    "BROWSER_RECAPTURE_BINDING_INVALID",
                    "Unit response recapture requires complete rendered-text bindings",
                )
            conversation = BrowserConversationRepository(connection).get(
                item.conversation_id
            )
            if (
                conversation.project_id != project.id
                or conversation.context_id != item.context_id
                or conversation.status is not ConversationStatus.READY
                or conversation.conversation_path is None
                or not is_real_conversation_path(conversation.conversation_path)
                or conversation.first_turn_fingerprint is None
            ):
                raise IntegrityError(
                    "BROWSER_RECAPTURE_CONVERSATION_INVALID",
                    "Response recapture requires the exact proven ready conversation",
                )
            preparation = PreparationRepository(connection).get(item.preparation_id)
            preparation_run = StageRunRepository(connection).get(
                preparation.stage_run_id
            )
            if (
                preparation.project_id != project.id
                or preparation.context_id != item.context_id
                or preparation.unit_id != item.unit_id
                or preparation.request_artifact_id != item.request_artifact_id
                or preparation.request_sha256 != item.request_sha256
                or preparation_run.status is not RunStatus.DONE
            ):
                raise IntegrityError(
                    "BROWSER_RECAPTURE_PREPARATION_MISMATCH",
                    "Interaction differs from its exact completed unit preparation",
                )
            response_artifact = next(
                (
                    artifact
                    for artifact in ArtifactRepository(connection).list_for_project(
                        project.id
                    )
                    if artifact.id == item.response_artifact_id
                ),
                None,
            )
            if (
                response_artifact is None
                or response_artifact.stage_run_id != item.stage_run_id
                or response_artifact.artifact_type != "browser_response_raw"
                or response_artifact.sha256 != item.response_sha256
            ):
                raise IntegrityError(
                    "BROWSER_RESPONSE_ARTIFACT_INVALID",
                    "Persisted response artifact owner or binding differs",
                )
            run = StageRunRepository(connection).get(item.stage_run_id)
            expected_run_status = (
                RunStatus.RUNNING
                if item.status is InteractionStatus.CAPTURED
                else RunStatus.DONE
            )
            if run.status is not expected_run_status:
                raise IntegrityError(
                    "BROWSER_STAGE_RUN_DIVERGENT",
                    "Interaction and StageRun disagree at response recapture",
                )
            if item.status is InteractionStatus.CAPTURED:
                if (
                    item.imported_entity_type is not None
                    or item.imported_entity_id is not None
                    or item.imported_at is not None
                ):
                    raise IntegrityError(
                        "BROWSER_RECAPTURE_BINDING_INVALID",
                        "Captured interaction contains partial import identity",
                    )
                previous_submission = None
            else:
                if (
                    item.imported_entity_type != "text_unit_submission"
                    or item.imported_entity_id is None
                    or item.imported_at is None
                ):
                    raise IntegrityError(
                        "BROWSER_RECAPTURE_BINDING_INVALID",
                        "Imported interaction lacks its exact M2 submission identity",
                    )
                previous_submission = SubmissionRepository(connection).get(
                    item.imported_entity_id
                )
                if (
                    previous_submission.project_id != project.id
                    or previous_submission.context_id != item.context_id
                    or previous_submission.preparation_id != item.preparation_id
                    or previous_submission.unit_id != item.unit_id
                    or previous_submission.raw_sha256 != item.response_sha256
                    or previous_submission.raw_artifact_id is None
                ):
                    raise IntegrityError(
                        "BROWSER_RECAPTURE_IMPORT_IDENTITY_DIVERGENT",
                        "Persisted M2 submission differs from the response binding",
                    )
            events = interactions.events(item.id)
            user_turn_ordinal = self._persisted_unit_turn_ordinal(
                item, conversation, events
            )
        self._response_bytes(project_id, item)
        if self.config.browser_capture_method_version != "rendered_text_v1":
            raise ConflictError(
                "BROWSER_RECAPTURE_CAPTURE_METHOD_INVALID",
                "Response recapture requires rendered_text_v1",
            )
        session = self.adapter.ensure_ready()
        if session is not SessionState.READY:
            raise ConflictError(
                "BROWSER_RECAPTURE_SESSION_NOT_READY",
                "The dedicated browser profile is not ready for response recapture",
                evidence={"session_state": session.value},
            )
        if not self.adapter.capture_gate_ready():
            raise ConflictError(
                "BROWSER_CAPTURE_SPIKE_REQUIRED",
                "The rendered_text_v1 capture gate is not approved",
            )
        conversation_path = conversation.conversation_path
        assert conversation_path is not None
        self.adapter.open_conversation(conversation_path)
        captured = captured_response_bytes(
            self.adapter.recapture_persisted_unit_response(user_turn_ordinal)
        )
        if not captured.strip():
            raise ConflictError(
                "BROWSER_RECAPTURE_RESPONSE_EMPTY",
                "The recaptured rendered unit response is empty",
            )
        if self.adapter.current_conversation_path() != conversation_path:
            raise ConflictError(
                "BROWSER_RECAPTURE_WRONG_CONVERSATION",
                "Response recapture moved away from the persisted conversation",
            )
        response_sha = sha256_bytes(captured)
        if response_sha == item.response_sha256:
            if item.status is InteractionStatus.CAPTURED:
                result = self._import_captured(project_id, item, captured)
                outcome = "imported_existing_capture"
                imported_entity_id = result.get("imported_entity_id")
                assert isinstance(imported_entity_id, str)
                with self.database.connection() as connection:
                    submission = SubmissionRepository(connection).get(imported_entity_id)
            else:
                assert previous_submission is not None
                submission = previous_submission
                result = self._payload(item)
                result["disposition"] = submission.disposition.value
                result["paused"] = (
                    submission.disposition is not SubmissionDisposition.ACCEPTED
                )
                outcome = "already_current"
            return {
                **result,
                "outcome": outcome,
                "conversation_path": conversation_path,
                "user_turn_ordinal": user_turn_ordinal,
                "response_artifact_created": False,
                "raw_version": submission.raw_version,
            }
        stored = self.store.write_bytes(
            project.artifact_root,
            f"browser/responses/{item.id}/recapture-{response_sha}.txt",
            captured,
        )
        submission = self.writing.import_unit_by_preparation_id(
            project.id, item.preparation_id, captured
        )
        if (
            submission.project_id != project.id
            or submission.context_id != item.context_id
            or submission.preparation_id != item.preparation_id
            or submission.unit_id != item.unit_id
            or submission.raw_sha256 != response_sha
        ):
            raise IntegrityError(
                "BROWSER_RECAPTURE_M2_IMPORT_MISMATCH",
                "M2 recapture import differs from the persisted interaction binding",
            )
        with (
            self.database.connection() as connection,
            self.database.transaction(connection),
        ):
            current = BrowserInteractionRepository(connection).get(item.id)
            current_conversation = BrowserConversationRepository(connection).get(
                conversation.id
            )
            if (
                current.status is not item.status
                or current.response_artifact_id != item.response_artifact_id
                or current.response_sha256 != item.response_sha256
                or current.preparation_id != item.preparation_id
                or current.imported_entity_id != item.imported_entity_id
                or current_conversation.conversation_path != conversation_path
                or current_conversation.context_id != item.context_id
            ):
                raise ConflictError(
                    "BROWSER_RECAPTURE_CONCURRENT_UPDATE",
                    "Interaction bindings changed while the response was recaptured",
                )
            current, artifact = self.persistence.record_unit_response_recapture(
                connection,
                project,
                current,
                stored,
                submission_id=submission.id,
                raw_version=submission.raw_version,
                conversation_path=conversation_path,
                user_turn_ordinal=user_turn_ordinal,
            )
        LOGGER.info(
            "browser_recapture interaction_id=%s conversation_path=%s "
            "user_turn_ordinal=%s previous_response_sha256=%s response_sha256=%s "
            "response_artifact_id=%s preparation_id=%s imported_entity_id=%s raw_version=%s",
            current.id,
            conversation_path,
            user_turn_ordinal,
            item.response_sha256,
            response_sha,
            artifact.id,
            current.preparation_id,
            submission.id,
            submission.raw_version,
            extra={"operation": "browser.interaction.recapture_response", "status": "recaptured"},
        )
        result = self._payload(current)
        result["disposition"] = submission.disposition.value
        result["paused"] = submission.disposition is not SubmissionDisposition.ACCEPTED
        return {
            **result,
            "outcome": "recaptured",
            "conversation_path": conversation_path,
            "user_turn_ordinal": user_turn_ordinal,
            "previous_response_sha256": item.response_sha256,
            "response_artifact_created": True,
            "raw_version": submission.raw_version,
        }

    @staticmethod
    def _persisted_unit_turn_ordinal(
        item: BrowserInteraction,
        conversation: BrowserConversation,
        events: list[dict[str, object]],
    ) -> int:
        proofs: list[int] = []
        for event in events:
            evidence = event.get("evidence")
            if not isinstance(evidence, dict) or not (
                evidence.get("event_type") == "explicit_reconciliation"
                and evidence.get("proof_kind") == "persisted_unit_ordinal_v1"
            ):
                continue
            ordinal = evidence.get("user_turn_ordinal")
            if (
                isinstance(ordinal, bool)
                or not isinstance(ordinal, int)
                or ordinal < 0
                or evidence.get("conversation_path") != conversation.conversation_path
                or evidence.get("transport_fingerprint") != item.transport_fingerprint
            ):
                raise IntegrityError(
                    "BROWSER_RECAPTURE_TURN_PROOF_DIVERGENT",
                    "Persisted unit-turn proof differs from the interaction",
                )
            proofs.append(ordinal)
        if len(proofs) != 1:
            raise ConflictError(
                "BROWSER_RECAPTURE_TURN_PROOF_UNAVAILABLE",
                "Response recapture requires exactly one persisted ordinal turn proof",
                evidence={"persisted_turn_proof_count": len(proofs)},
            )
        return proofs[0]

    def status(self, project_id: str) -> dict[str, object]:
        with self.database.connection() as connection:
            ProjectRepository(connection).get(project_id)
            rows = connection.execute(
                "SELECT id FROM browser_conversations WHERE project_id = ? ORDER BY created_at",
                (project_id,),
            ).fetchall()
            conversations = [
                BrowserConversationRepository(connection).get(str(row["id"])) for row in rows
            ]
            invalidations = BrowserConversationInvalidationRepository(connection)
            conversation_payload = []
            for conversation in conversations:
                invalidation = invalidations.for_conversation(conversation.id)
                conversation_payload.append(
                    {
                        **asdict(conversation),
                        "status": conversation.status.value,
                        "reusable": (
                            invalidation is None
                            and conversation.status is ConversationStatus.READY
                            and conversation.conversation_path is not None
                            and is_real_conversation_path(conversation.conversation_path)
                        ),
                        "invalidation": (
                            None
                            if invalidation is None
                            else {
                                **asdict(invalidation),
                                "evidence": json.loads(invalidation.evidence_json),
                            }
                        ),
                    }
                )
            interactions = connection.execute(
                "SELECT status, count(*) AS count FROM browser_interactions "
                "WHERE project_id = ? GROUP BY status ORDER BY status",
                (project_id,),
            ).fetchall()
            interaction_repository = BrowserInteractionRepository(connection)
            resolution_repository = BrowserInteractionResolutionRepository(connection)
            all_interactions = interaction_repository.list_for_project(project_id)
            active_payload = [
                {
                    "interaction_id": item.id,
                    "conversation_id": item.conversation_id,
                    "context_id": item.context_id,
                    "kind": item.kind.value,
                    "unit_id": item.unit_id,
                    "status": item.status.value,
                    "created_at": item.created_at,
                }
                for item in all_interactions
                if item.status in ACTIVE_INTERACTION_STATUSES
            ]
            blocked = interaction_repository.list_blocked(project_id)
            blocked_payload = [
                {
                    "interaction_id": item.id,
                    "context_id": item.context_id,
                    "kind": item.kind.value,
                    "unit_id": item.unit_id,
                    "operator_abandoned": (
                        resolution_repository.for_interaction(item.id) is not None
                    ),
                }
                for item in blocked
            ]
        return {
            "project_id": project_id,
            "conversations": conversation_payload,
            "interaction_counts": {str(row["status"]): int(row["count"]) for row in interactions},
            "active_interactions": active_payload,
            "blocked_interactions": blocked_payload,
        }

    def interaction_show(
        self, project_id: str, interaction_id: str | None = None
    ) -> dict[str, object]:
        with self.database.connection() as connection:
            ProjectRepository(connection).get(project_id)
            interactions = BrowserInteractionRepository(connection)
            if interaction_id is None:
                all_interactions = interactions.list_for_project(project_id)
                active = [
                    item
                    for item in all_interactions
                    if item.status in ACTIVE_INTERACTION_STATUSES
                ]
                if len(active) > 1:
                    candidate_ids = [candidate.id for candidate in active]
                    raise ConflictError(
                        "BROWSER_INTERACTION_SELECTION_AMBIGUOUS",
                        "More than one active browser interaction requires an explicit ID: "
                        + ", ".join(candidate_ids),
                        evidence={
                            "candidates": [
                                {
                                    "interaction_id": candidate.id,
                                    "conversation_id": candidate.conversation_id,
                                    "created_at": candidate.created_at,
                                    "status": candidate.status.value,
                                }
                                for candidate in active
                            ]
                        },
                    )
                if active:
                    item = active[0]
                else:
                    unresolved = [
                        candidate
                        for candidate in all_interactions
                        if candidate.status is InteractionStatus.BLOCKED
                        and BrowserInteractionResolutionRepository(
                            connection
                        ).for_interaction(candidate.id)
                        is None
                    ]
                    if len(unresolved) > 1:
                        candidate_ids = [candidate.id for candidate in unresolved]
                        raise ConflictError(
                            "BROWSER_INTERACTION_SELECTION_AMBIGUOUS",
                            "More than one unresolved interaction requires an explicit ID: "
                            + ", ".join(candidate_ids),
                            evidence={
                                "candidates": [
                                    {
                                        "interaction_id": candidate.id,
                                        "conversation_id": candidate.conversation_id,
                                        "created_at": candidate.created_at,
                                        "status": candidate.status.value,
                                    }
                                    for candidate in unresolved
                                ]
                            },
                        )
                    if not unresolved:
                        raise NotFoundError(
                            "BROWSER_INTERACTION_NOT_FOUND",
                            "No active or unresolved browser interaction was found; provide an ID",
                        )
                    item = unresolved[0]
            else:
                item = interactions.get(interaction_id)
                if item.project_id != project_id:
                    raise NotFoundError(
                        "BROWSER_INTERACTION_NOT_FOUND", "Browser interaction was not found"
                    )
            conversation = BrowserConversationRepository(connection).get(item.conversation_id)
            stage_run = StageRunRepository(connection).get(item.stage_run_id)
            resolution = BrowserInteractionResolutionRepository(connection).for_interaction(
                item.id
            )
            conversation_invalidation = BrowserConversationInvalidationRepository(
                connection
            ).for_conversation(conversation.id)
            events = interactions.events(item.id)
        return {
            "interaction": self._payload(item),
            "stage_run": {**asdict(stage_run), "status": stage_run.status.value},
            "conversation": {
                **asdict(conversation),
                "status": conversation.status.value,
                "reusable": (
                    conversation_invalidation is None
                    and conversation.status is ConversationStatus.READY
                    and conversation.conversation_path is not None
                    and is_real_conversation_path(conversation.conversation_path)
                ),
                "invalidation": (
                    None
                    if conversation_invalidation is None
                    else {
                        **asdict(conversation_invalidation),
                        "evidence": json.loads(conversation_invalidation.evidence_json),
                    }
                ),
            },
            "events": events,
            "operator_resolution": self._resolution_payload(resolution),
        }

    def abandon_interaction(
        self,
        project_id: str,
        interaction_id: str,
        *,
        operator: str,
        reason: str,
    ) -> dict[str, object]:
        operator = operator.strip()
        reason = reason.strip()
        if not operator:
            raise ConflictError(
                "BROWSER_ABANDON_OPERATOR_REQUIRED",
                "Operator identity is required for an abandonment decision",
            )
        if not reason:
            raise ConflictError(
                "BROWSER_ABANDON_REASON_REQUIRED",
                "A reason is required for an abandonment decision",
            )
        with (
            self.database.connection() as connection,
            self.database.transaction(connection),
        ):
            ProjectRepository(connection).get(project_id)
            interactions = BrowserInteractionRepository(connection)
            item = interactions.get(interaction_id)
            if item.project_id != project_id:
                raise NotFoundError(
                    "BROWSER_INTERACTION_NOT_FOUND", "Browser interaction was not found"
                )
            if item.status is not InteractionStatus.BLOCKED:
                raise ConflictError(
                    "BROWSER_ABANDON_STATE_INVALID",
                    "Only a blocked or ambiguous browser interaction can be abandoned",
                    evidence={"interaction_id": item.id, "status": item.status.value},
                )
            resolutions = BrowserInteractionResolutionRepository(connection)
            existing = resolutions.for_interaction(item.id)
            if existing is not None:
                if existing.operator == operator and existing.reason == reason:
                    existing_payload = self._resolution_payload(existing)
                    assert existing_payload is not None
                    return existing_payload
                raise ConflictError(
                    "BROWSER_INTERACTION_ALREADY_RESOLVED",
                    "Browser interaction already has an operator resolution",
                    evidence={"interaction_id": item.id, "resolution_id": existing.id},
                )
            conversation = BrowserConversationRepository(connection).get(item.conversation_id)
            recoverable_statuses = {
                InteractionStatus.PREPARED,
                InteractionStatus.SENDING,
                InteractionStatus.SENT,
                InteractionStatus.STREAMING,
                InteractionStatus.CAPTURED,
            }
            other_recoverable = [
                value
                for value in interactions.list_for_conversation(conversation.id)
                if value.id != item.id and value.status in recoverable_statuses
            ]
            close_provisioning = (
                conversation.status is ConversationStatus.PROVISIONING
                and conversation.conversation_path is None
                and conversation.first_turn_fingerprint is None
                and not other_recoverable
            )
            action = (
                ConversationResolutionAction.PROVISIONING_ATTEMPT_CLOSED
                if close_provisioning
                else ConversationResolutionAction.RETAINED
            )
            snapshot = {
                **asdict(conversation),
                "status": conversation.status.value,
            }
            if close_provisioning:
                self.persistence.close_provisioning_conversation(connection, conversation)
            now = utc_now()
            resolution = BrowserInteractionResolution(
                id=new_id(),
                interaction_id=item.id,
                project_id=item.project_id,
                resolution=InteractionResolutionKind.OPERATOR_ABANDONED,
                operator=operator,
                reason=reason,
                conversation_action=action,
                evidence_json=json.dumps(
                    {
                        "decision_scope": "external_send_outcome_unprovable",
                        "conversation_snapshot": snapshot,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                created_at=now,
            )
            resolutions.add(resolution)
            interactions.event(
                item.id,
                item.project_id,
                item.status,
                item.status,
                {
                    "event_type": "operator_resolution",
                    "resolution_id": resolution.id,
                    "resolution": resolution.resolution.value,
                    "conversation_action": resolution.conversation_action.value,
                },
            )
        payload = self._resolution_payload(resolution)
        assert payload is not None
        return payload

    def validate(self, project_id: str) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        with self.database.connection() as connection:
            project = ProjectRepository(connection).get(project_id)
            rows = connection.execute(
                "SELECT id FROM browser_interactions WHERE project_id = ?", (project_id,)
            ).fetchall()
            repository = BrowserInteractionRepository(connection)
            conversation_rows = connection.execute(
                "SELECT c.id, c.conversation_path, i.id AS invalidation_id "
                "FROM browser_conversations c LEFT JOIN browser_conversation_invalidations i "
                "ON i.conversation_id = c.id WHERE c.project_id = ?",
                (project_id,),
            ).fetchall()
            for conversation_row in conversation_rows:
                path = conversation_row["conversation_path"]
                if (
                    conversation_row["invalidation_id"] is None
                    and path is not None
                    and not is_real_conversation_path(str(path))
                ):
                    issues.append(
                        ValidationIssue(
                            "BROWSER_CONVERSATION_PATH_INVALID",
                            "Persisted conversation path is not a canonical /c/<uuid> path",
                            relative_path=str(conversation_row["id"]),
                        )
                    )
            for row in rows:
                item = repository.get(str(row["id"]))
                try:
                    request_row = connection.execute(
                        "SELECT sha256, relative_path, byte_size FROM artifacts WHERE id = ? "
                        "AND project_id = ?",
                        (item.request_artifact_id, project.id),
                    ).fetchone()
                    if request_row is None or str(request_row["sha256"]) != item.request_sha256:
                        raise IntegrityError(
                            "BROWSER_REQUEST_ARTIFACT_INVALID", "Request artifact binding differs"
                        )
                    inspected = self.store.inspect(
                        project.artifact_root, str(request_row["relative_path"])
                    )
                    if (
                        inspected.sha256 != str(request_row["sha256"])
                        or inspected.byte_size != int(request_row["byte_size"])
                    ):
                        raise IntegrityError(
                            "BROWSER_REQUEST_ARTIFACT_INVALID", "Request artifact bytes differ"
                        )
                    if item.status in {InteractionStatus.CAPTURED, InteractionStatus.IMPORTED}:
                        self._response_bytes(project.id, item)
                    run = StageRunRepository(connection).get(item.stage_run_id)
                    if item.status is InteractionStatus.IMPORTED and run.status.value != "done":
                        raise IntegrityError(
                            "BROWSER_STAGE_RUN_DIVERGENT", "Imported interaction run is not done"
                        )
                except HeliosError as exc:
                    issues.append(ValidationIssue(exc.code, exc.message))
        return issues

    def _observe_capture_import(
        self,
        project_id: str,
        item: BrowserInteraction,
        conversation: BrowserConversation,
        *,
        structural_happy_path: bool = False,
    ) -> dict[str, object]:
        if conversation.conversation_path is not None:
            if not is_real_conversation_path(conversation.conversation_path):
                raise IntegrityError(
                    "BROWSER_CONVERSATION_PATH_INVALID",
                    "Persisted conversation path is not a canonical /c/<uuid> path",
                )
            if not structural_happy_path:
                self.adapter.open_conversation(conversation.conversation_path)
        elif conversation.status is not ConversationStatus.PROVISIONING:
            raise IntegrityError(
                "BROWSER_BOOTSTRAP_RECOVERY_REQUIRED",
                "Conversation has no proven page URL; run browser recover",
                recoverable=True,
            )
        user_turn_proven = item.status in {
            InteractionStatus.SENT,
            InteractionStatus.STREAMING,
        }
        proof_deadline = monotonic() + self.config.browser_timeout_seconds
        response_deadline = (
            monotonic() + self.config.browser_timeout_seconds
            if user_turn_proven
            else None
        )
        while True:
            inspection = (
                self.adapter.inspect_sent_turn_structure(item.transport_fingerprint)
                if structural_happy_path
                else self.adapter.inspect_turn(item.transport_fingerprint)
            )
            if (
                inspection.conversation_path is not None
                and not is_real_conversation_path(inspection.conversation_path)
            ):
                self._block(
                    item,
                    "BROWSER_CONVERSATION_PATH_INVALID",
                    "Observed page URL did not contain a canonical /c/<uuid> path",
                )
            if (
                conversation.conversation_path is not None
                and inspection.conversation_path not in {None, conversation.conversation_path}
            ):
                self._block(
                    item,
                    "BROWSER_WRONG_CONVERSATION",
                    "Observed response belongs to another conversation",
                )
            if inspection.state is TurnState.AMBIGUOUS:
                self._block(
                    item,
                    "BROWSER_TURN_AMBIGUOUS",
                    "Browser state cannot prove the requested user turn",
                    inspection.evidence,
                )
            if inspection.state is TurnState.NOT_SENT:
                active_deadline = response_deadline if user_turn_proven else proof_deadline
                assert active_deadline is not None
                if monotonic() >= active_deadline:
                    if user_turn_proven:
                        self._block(
                            item,
                            "BROWSER_RESPONSE_TIMEOUT",
                            "Previously proven turn did not reach a complete response in time",
                        )
                    self._block(
                        item,
                        "BROWSER_SEND_NOT_PROVABLE",
                        "Browser could not prove the user turn before the configured timeout",
                    )
                sleep(0.25)
                continue
            if not user_turn_proven:
                if inspection.observed_user_turn_fingerprint != item.transport_fingerprint:
                    self._block(
                        item,
                        "BROWSER_USER_TURN_FINGERPRINT_MISMATCH",
                        "Adapter did not provide the matching fingerprint of an observed user turn",
                    )
                observed_path = self.adapter.current_conversation_path()
                if observed_path is None:
                    if monotonic() >= proof_deadline:
                        self._block(
                            item,
                            "BROWSER_CONVERSATION_PATH_NOT_OBSERVED",
                            "A matching user turn exists but page.url has no real "
                            "conversation path",
                        )
                    sleep(0.25)
                    continue
                if not is_real_conversation_path(observed_path):
                    self._block(
                        item,
                        "BROWSER_CONVERSATION_PATH_INVALID",
                        "Observed page URL did not contain a canonical /c/<uuid> path",
                    )
                if inspection.conversation_path not in {None, observed_path}:
                    self._block(
                        item,
                        "BROWSER_WRONG_CONVERSATION",
                        "Observed user turn belongs to another conversation",
                    )
                with (
                    self.database.connection() as connection,
                    self.database.transaction(connection),
                ):
                    current = BrowserInteractionRepository(connection).get(item.id)
                    current_conversation = BrowserConversationRepository(connection).get(
                        conversation.id
                    )
                    item, conversation = self.persistence.prove_user_turn_and_bind_conversation(
                        connection,
                        current,
                        current_conversation,
                        observed_path,
                        inspection.observed_user_turn_fingerprint,
                        structural_evidence=(
                            self._local_successor_persistence_evidence(
                                inspection.evidence
                            )
                            if item.kind is InteractionKind.UNIT_REQUEST
                            and isinstance(inspection.evidence, dict)
                            else None
                        ),
                    )
                user_turn_proven = True
                response_deadline = monotonic() + self.config.browser_timeout_seconds
                LOGGER.debug(
                    "browser_send checkpoint=user_turn_observed",
                    extra={
                        "operation": "browser.send_message",
                        "status": "user_turn_observed",
                    },
                )
            if inspection.state is TurnState.STREAMING:
                if item.status is InteractionStatus.SENT:
                    with (
                        self.database.connection() as connection,
                        self.database.transaction(connection),
                    ):
                        item = self.persistence.transition(
                            connection, item, InteractionStatus.STREAMING
                        )
                    self._checkpoint("browser.streaming")
                assert response_deadline is not None
                if monotonic() >= response_deadline:
                    self._block(
                        item,
                        "BROWSER_RESPONSE_TIMEOUT",
                        "Response did not reach a proven complete state before timeout",
                    )
                sleep(0.25)
                continue
            if inspection.state is TurnState.COMPLETE:
                captured = captured_response_bytes(
                    self.adapter.capture_structural_response(item.transport_fingerprint)
                    if structural_happy_path
                    else self.adapter.capture_response(item.transport_fingerprint)
                )
                if not captured.strip():
                    self._block(item, "BROWSER_RESPONSE_EMPTY", "Captured response is empty")
                stored = self.store.write_bytes(
                    self._project_root(project_id),
                    f"browser/responses/{item.id}/attempt-{item.attempt:04d}.txt",
                    captured,
                )
                self._checkpoint("browser.response_stored")
                with self.database.connection() as connection:
                    project = ProjectRepository(connection).get(project_id)
                    with self.database.transaction(connection):
                        item = self.persistence.register_response(
                            connection,
                            project,
                            item,
                            stored,
                            self.config.browser_capture_method_version,
                        )
                        item = self.persistence.transition(
                            connection, item, InteractionStatus.CAPTURED
                        )
                self._checkpoint("browser.response_captured")
                return self._import_captured(project_id, item, captured)

    def _blocked_reprobe_result(
        self, item: BrowserInteraction, reason: str, *, observations: int
    ) -> dict[str, object]:
        with (
            self.database.connection() as connection,
            self.database.transaction(connection),
        ):
            current = BrowserInteractionRepository(connection).get(item.id)
            if current.status is InteractionStatus.BLOCKED:
                BrowserInteractionRepository(connection).event(
                    current.id,
                    current.project_id,
                    current.status,
                    current.status,
                    {
                        "event_type": "recovery_reprobe",
                        "outcome": "still_blocked",
                        "reason": reason,
                        "observations": observations,
                    },
                )
        return {
            "outcome": "still_blocked",
            "interaction_id": item.id,
            "reason": reason,
            "observations": observations,
        }

    @staticmethod
    def _last_blocking_error_code(events: list[dict[str, object]]) -> str | None:
        for event in reversed(events):
            if event.get("status") != InteractionStatus.BLOCKED.value:
                continue
            evidence = event.get("evidence")
            if isinstance(evidence, dict):
                value = evidence.get("error_code")
                return None if value is None else str(value)
        return None

    @staticmethod
    def _send_attempt_started_at(events: list[dict[str, object]]) -> str:
        for event in reversed(events):
            evidence = event.get("evidence")
            if (
                isinstance(evidence, dict)
                and evidence.get("effect_boundary") == "send_attempt_started"
            ):
                value = event.get("created_at")
                if isinstance(value, str) and value:
                    return value
        raise ConflictError(
            "BROWSER_RECONCILE_BOUNDARY_MISSING",
            "Blocked reconciliation source has no send-attempt-started boundary",
        )

    @staticmethod
    def _validated_unit_turn_anchor(
        anchor: object,
        *,
        expected_conversation_path: str | None,
        error_code: str = "BROWSER_PRE_SEND_TURN_ANCHOR_UNAVAILABLE",
    ) -> dict[str, object]:
        if not isinstance(anchor, dict):
            raise ConflictError(
                error_code,
                "Unit turn binding requires a persisted structural pre-Send tail anchor",
            )
        tail = anchor.get("tail")
        user_count = anchor.get("pre_send_user_turn_count")
        assistant_count = anchor.get("pre_send_assistant_turn_count")
        valid_tail = (
            isinstance(tail, list)
            and len(tail) == 2
            and all(isinstance(entry, dict) for entry in tail)
        )
        if not valid_tail:
            raise ConflictError(
                error_code,
                "Unit turn binding requires one stable user/assistant tail pair",
            )
        assert isinstance(tail, list)
        first = tail[0]
        second = tail[1]
        assert isinstance(first, dict) and isinstance(second, dict)
        first_id = first.get("id")
        second_id = second.get("id")
        if (
            anchor.get("version") != 1
            or anchor.get("conversation_path") != expected_conversation_path
            or expected_conversation_path is None
            or not is_real_conversation_path(expected_conversation_path)
            or not isinstance(user_count, int)
            or user_count < 1
            or not isinstance(assistant_count, int)
            or assistant_count < 1
            or first.get("role") != "user"
            or second.get("role") != "assistant"
            or not isinstance(first_id, str)
            or not first_id
            or not isinstance(second_id, str)
            or not second_id
            or first_id == second_id
        ):
            raise ConflictError(
                error_code,
                "Unit turn tail anchor has missing or divergent structural identities",
            )
        return {
            "version": 1,
            "conversation_path": expected_conversation_path,
            "pre_send_user_turn_count": user_count,
            "pre_send_assistant_turn_count": assistant_count,
            "tail": [
                {"role": "user", "id": first_id},
                {"role": "assistant", "id": second_id},
            ],
        }

    @staticmethod
    def _reconciliation_baseline(value: str | None) -> set[str]:
        if value is None:
            raise ConflictError(
                "BROWSER_RECONCILE_BASELINE_MISSING",
                "Reconciliation requires the persisted pre-send conversation baseline",
            )
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise IntegrityError(
                "BROWSER_RECONCILE_BASELINE_INVALID",
                "Persisted pre-send conversation baseline is invalid JSON",
            ) from exc
        if not isinstance(parsed, list) or any(not isinstance(item, str) for item in parsed):
            raise IntegrityError(
                "BROWSER_RECONCILE_BASELINE_INVALID",
                "Persisted pre-send conversation baseline must be a string list",
            )
        return set(parsed)

    @staticmethod
    def _timestamp_at_or_after(observed_at: str, started_at: str) -> bool:
        try:
            observed = datetime.fromisoformat(observed_at)
            started = datetime.fromisoformat(started_at)
        except ValueError:
            return False
        if observed.tzinfo is None or started.tzinfo is None:
            return False
        return observed >= started

    def _import_captured(
        self, project_id: str, item: BrowserInteraction, response: bytes | None = None
    ) -> dict[str, object]:
        if response is None:
            response = self._response_bytes(project_id, item)
        if sha256_bytes(response) != item.response_sha256:
            self._block(
                item,
                "BROWSER_RESPONSE_HASH_MISMATCH",
                "Captured bytes differ from the registered response artifact",
            )
        if item.kind is InteractionKind.CONTEXT_LOAD:
            acknowledgement = self.writing.acknowledge_context_by_id(
                project_id, item.context_id, response
            )
            entity_type = "writing_acknowledgement"
            entity_id = acknowledgement.id
            disposition: SubmissionDisposition | None = None
            if acknowledgement.raw_sha256 != item.response_sha256:
                raise IntegrityError(
                    "BROWSER_M2_IMPORT_HASH_MISMATCH", "M2 acknowledgement bytes differ"
                )
        else:
            assert item.preparation_id is not None
            submission = self.writing.import_unit_by_preparation_id(
                project_id, item.preparation_id, response
            )
            entity_type = "text_unit_submission"
            entity_id = submission.id
            disposition = submission.disposition
            if submission.raw_sha256 != item.response_sha256:
                raise IntegrityError("BROWSER_M2_IMPORT_HASH_MISMATCH", "M2 raw bytes differ")
        self._checkpoint("browser.after_m2_import")
        with (
            self.database.connection() as connection,
            self.database.transaction(connection),
        ):
            item = self.persistence.set_import_identity(
                connection, item, entity_type, entity_id
            )
            item = self.persistence.transition(connection, item, InteractionStatus.IMPORTED)
        payload = self._payload(item)
        if entity_type == "text_unit_submission":
            assert disposition is not None
            payload["disposition"] = disposition.value
            payload["paused"] = disposition is not SubmissionDisposition.ACCEPTED
        else:
            payload["confirmation_required"] = True
        return payload

    def _request_bytes(self, project_id: str, item: BrowserInteraction) -> bytes:
        with self.database.connection() as connection:
            project = ProjectRepository(connection).get(project_id)
            artifact = ArtifactRepository(connection).list_for_project(project.id)
            bound = next(
                (value for value in artifact if value.id == item.request_artifact_id), None
            )
            if bound is None or bound.sha256 != item.request_sha256:
                raise IntegrityError(
                    "BROWSER_REQUEST_ARTIFACT_INVALID", "Request artifact binding differs"
                )
            inspected = self.store.inspect(project.artifact_root, bound.relative_path)
            if inspected.sha256 != bound.sha256 or inspected.byte_size != bound.byte_size:
                raise IntegrityError(
                    "BROWSER_REQUEST_ARTIFACT_INVALID", "Request artifact bytes differ"
                )
            return self.store.resolve(project.artifact_root, bound.relative_path).read_bytes()

    def _response_bytes(self, project_id: str, item: BrowserInteraction) -> bytes:
        if item.response_artifact_id is None or item.response_sha256 is None:
            raise IntegrityError(
                "BROWSER_RESPONSE_ARTIFACT_MISSING", "Response artifact is not registered"
            )
        with self.database.connection() as connection:
            project = ProjectRepository(connection).get(project_id)
            artifacts = ArtifactRepository(connection).list_for_project(project.id)
            artifact = next(
                (value for value in artifacts if value.id == item.response_artifact_id), None
            )
            if artifact is None or artifact.sha256 != item.response_sha256:
                raise IntegrityError(
                    "BROWSER_RESPONSE_ARTIFACT_INVALID", "Response artifact binding differs"
                )
            inspected = self.store.inspect(project.artifact_root, artifact.relative_path)
            if inspected.sha256 != artifact.sha256 or inspected.byte_size != artifact.byte_size:
                raise IntegrityError(
                    "BROWSER_RESPONSE_ARTIFACT_INVALID", "Response artifact bytes differ"
                )
            return self.store.resolve(project.artifact_root, artifact.relative_path).read_bytes()

    def _assert_automation_context(self, project_id: str, context_id: str) -> None:
        context = self.writing.contexts.get_by_id(project_id, context_id)
        contract = self.writing.contexts.resolve_contract(context)
        with self.database.connection() as connection:
            required = len(contract.model.units)
            row = connection.execute(
                "SELECT count(*) FROM writing_unit_academic_projections WHERE context_id = ?",
                (context_id,),
            ).fetchone()
            if row is None or int(row[0]) != required:
                raise ConflictError(
                    "WRITING_CONTEXT_NOT_AUTOMATION_READY",
                    "WritingContext lacks eager projections for every contract unit",
                )

    def _assert_browser_enabled(self, project_id: str) -> None:
        runtime = self.runtime_config.resolve(project_id)
        if not runtime.model.browser_automation_enabled:
            raise ConflictError(
                "BROWSER_AUTOMATION_DISABLED", "Project browser automation is disabled"
            )

    def _ensure_session_or_block(self, item: BrowserInteraction) -> None:
        state = self.adapter.ensure_ready()
        if state is SessionState.READY:
            return
        code = {
            SessionState.LOGIN_REQUIRED: "BROWSER_LOGIN_REQUIRED",
            SessionState.CHALLENGE: "BROWSER_SECURITY_CHALLENGE",
            SessionState.EXPIRED: "BROWSER_SESSION_EXPIRED",
        }[state]
        self._block(item, code, "Browser session requires explicit operator action")

    def _persist_post_send_reconciliation_failure(
        self, interaction_id: str, error: HeliosError
    ) -> None:
        if error.code not in POST_SEND_RECONCILIATION_ERROR_CODES:
            return
        with (
            self.database.connection() as connection,
            self.database.transaction(connection),
        ):
            interactions = BrowserInteractionRepository(connection)
            current = interactions.get(interaction_id)
            if current.status is InteractionStatus.BLOCKED:
                return
            if current.status is not InteractionStatus.SENDING:
                return
            blocked = self.persistence.transition(
                connection,
                current,
                InteractionStatus.BLOCKED,
                evidence={
                    **(error.context.evidence or {}),
                    "error_code": error.code,
                },
            )
            ErrorRepository(connection).add(
                project_id=blocked.project_id,
                stage_run_id=blocked.stage_run_id,
                code=error.code,
                message=error.message,
                recoverable=False,
                evidence=error.context.evidence,
            )

    @staticmethod
    def _has_send_attempt_started_event(events: list[dict[str, object]]) -> bool:
        for event in events:
            evidence = event.get("evidence")
            if (
                isinstance(evidence, dict)
                and evidence.get("effect_boundary") == "send_attempt_started"
            ):
                return True
        return False

    def _block(
        self,
        item: BrowserInteraction,
        code: str,
        message: str,
        evidence: dict[str, object] | None = None,
    ) -> NoReturn:
        with self.database.connection() as connection:
            current = BrowserInteractionRepository(connection).get(item.id)
            if current.status not in {InteractionStatus.BLOCKED, InteractionStatus.IMPORTED}:
                with self.database.transaction(connection):
                    self.persistence.transition(
                        connection,
                        current,
                        InteractionStatus.BLOCKED,
                        evidence={"error_code": code, **(evidence or {})},
                    )
                    ErrorRepository(connection).add(
                        project_id=current.project_id,
                        stage_run_id=current.stage_run_id,
                        code=code,
                        message=message,
                        recoverable=False,
                        evidence=evidence,
                    )
        raise IntegrityError(code, message, evidence=evidence)

    def _project_root(self, project_id: str) -> str:
        with self.database.connection() as connection:
            return ProjectRepository(connection).get(project_id).artifact_root

    def _checkpoint(self, name: str) -> None:
        if self.fault_hook is not None:
            self.fault_hook(name)

    @staticmethod
    def _payload(item: BrowserInteraction) -> dict[str, object]:
        return {**asdict(item), "status": item.status.value, "kind": item.kind.value}

    @staticmethod
    def _resolution_payload(
        resolution: BrowserInteractionResolution | None,
    ) -> dict[str, object] | None:
        if resolution is None:
            return None
        return {
            "id": resolution.id,
            "interaction_id": resolution.interaction_id,
            "project_id": resolution.project_id,
            "resolution": resolution.resolution.value,
            "operator": resolution.operator,
            "reason": resolution.reason,
            "conversation_action": resolution.conversation_action.value,
            "evidence": json.loads(resolution.evidence_json),
            "created_at": resolution.created_at,
        }

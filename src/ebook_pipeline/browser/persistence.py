from __future__ import annotations

import json
import sqlite3
from dataclasses import replace

from ebook_pipeline.browser.models import (
    BrowserConversation,
    BrowserConversationInvalidation,
    BrowserInteraction,
    ConversationStatus,
    InteractionKind,
    InteractionStatus,
)
from ebook_pipeline.browser.repositories import (
    BrowserConversationInvalidationRepository,
    BrowserConversationRepository,
    BrowserInteractionRepository,
    BrowserInteractionResolutionRepository,
)
from ebook_pipeline.browser.state_machine import BrowserInteractionStateMachine
from ebook_pipeline.browser.urls import is_real_conversation_path
from ebook_pipeline.config import AppConfig
from ebook_pipeline.core.errors import ConflictError
from ebook_pipeline.core.hashing import canonical_hash
from ebook_pipeline.core.ids import new_id, utc_now
from ebook_pipeline.core.models import Artifact, Project, RunStatus, StageRun, StoredFile
from ebook_pipeline.core.state_machine import StateMachine
from ebook_pipeline.storage.repositories import ArtifactRepository, StageRunRepository

BROWSER_STAGE = "chatgpt_browser_automation"


class BrowserPersistence:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.interaction_states = BrowserInteractionStateMachine()
        self.run_states = StateMachine()

    def conversation(
        self, connection: sqlite3.Connection, project_id: str, context_id: str
    ) -> BrowserConversation:
        repository = BrowserConversationRepository(connection)
        existing = repository.for_context(context_id)
        if existing is not None and (
            existing.conversation_path is None
            or is_real_conversation_path(existing.conversation_path)
        ):
            return existing
        if existing is not None:
            interactions = BrowserInteractionRepository(connection).list_for_conversation(
                existing.id
            )
            resolutions = BrowserInteractionResolutionRepository(connection)
            resolved = [resolutions.for_interaction(item.id) for item in interactions]
            if (
                not interactions
                or any(item.status is not InteractionStatus.BLOCKED for item in interactions)
                or any(resolution is None for resolution in resolved)
            ):
                raise ConflictError(
                    "BROWSER_INVALID_CONVERSATION_REQUIRES_RESOLUTION",
                    "Invalid legacy conversation has interactions without operator abandonment",
                    evidence={"conversation_id": existing.id},
                )
            replacement = self._new_conversation(project_id, context_id)
            repository.add(replacement)
            BrowserConversationInvalidationRepository(connection).add(
                BrowserConversationInvalidation(
                    id=new_id(),
                    conversation_id=existing.id,
                    project_id=project_id,
                    replacement_conversation_id=replacement.id,
                    reason="legacy_invalid_conversation_path",
                    evidence_json=json.dumps(
                        {
                            "conversation_path": existing.conversation_path,
                            "first_turn_fingerprint": existing.first_turn_fingerprint,
                            "interaction_ids": [item.id for item in interactions],
                            "resolution_ids": [
                                resolution.id for resolution in resolved if resolution is not None
                            ],
                            "status": existing.status.value,
                            "validation": "conversation_url_v1",
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    created_at=utc_now(),
                )
            )
            return replacement
        item = self._new_conversation(project_id, context_id)
        repository.add(item)
        return item

    @staticmethod
    def _new_conversation(project_id: str, context_id: str) -> BrowserConversation:
        now = utc_now()
        return BrowserConversation(
            id=new_id(),
            project_id=project_id,
            context_id=context_id,
            provider="chatgpt_web",
            status=ConversationStatus.PROVISIONING,
            conversation_path=None,
            first_turn_fingerprint=None,
            provisioning_baseline_json=None,
            provisioning_started_at=now,
            ready_at=None,
            created_at=now,
            updated_at=now,
        )

    def create_interaction(
        self,
        connection: sqlite3.Connection,
        *,
        project_id: str,
        conversation_id: str,
        context_id: str,
        kind: InteractionKind,
        request_artifact_id: str,
        request_sha256: str,
        transport_fingerprint: str,
        unit_id: str | None = None,
        preparation_id: str | None = None,
        attempt: int = 0,
        supersedes_interaction_id: str | None = None,
        stage_run: StageRun | None = None,
    ) -> BrowserInteraction:
        repository = BrowserInteractionRepository(connection)
        existing = repository.by_request(conversation_id, request_sha256)
        lineage_predecessor: BrowserInteraction | None = None
        if existing is not None:
            resolution = BrowserInteractionResolutionRepository(connection).for_interaction(
                existing.id
            )
            if existing.status is not InteractionStatus.BLOCKED or resolution is None:
                return existing
            if existing.attempt >= existing.max_attempts:
                raise ConflictError(
                    "BROWSER_ATTEMPTS_EXHAUSTED",
                    "Browser interaction attempt limit was reached",
                    evidence={"interaction_id": existing.id},
                )
            active = {
                InteractionStatus.PREPARED,
                InteractionStatus.SENDING,
                InteractionStatus.SENT,
                InteractionStatus.STREAMING,
                InteractionStatus.CAPTURED,
            }
            others = [
                value
                for value in repository.list_for_conversation(conversation_id)
                if value.id != existing.id and value.status in active
            ]
            if others:
                raise ConflictError(
                    "BROWSER_RECOVERY_REQUIRED",
                    "Another recoverable browser interaction still exists",
                    evidence={"interaction_ids": [value.id for value in others]},
                )
            conversation = BrowserConversationRepository(connection).get(conversation_id)
            if (
                resolution.conversation_action.value == "provisioning_attempt_closed"
                and conversation.status is ConversationStatus.BLOCKED
            ):
                self.reprovision_conversation(connection, conversation)
            previous_run = StageRunRepository(connection).get(existing.stage_run_id)
            stage_run = self.run_states.new_run(
                project_id=project_id,
                stage_id=BROWSER_STAGE,
                unit_id=("context_load" if unit_id is None else f"unit:{unit_id}"),
                input_hash=previous_run.input_hash,
                max_attempts=existing.max_attempts,
                version=previous_run.version + 1,
                supersedes_run_id=previous_run.id,
            )
            StageRunRepository(connection).add(stage_run)
            attempt = existing.attempt + 1
            supersedes_interaction_id = existing.id
        elif kind is InteractionKind.CONTEXT_LOAD:
            applicable = [
                item
                for item in repository.list_for_project(project_id)
                if item.context_id == context_id
                and item.kind is kind
                and item.unit_id is None
                and item.request_sha256 == request_sha256
            ]
            if applicable:
                previous = applicable[-1]
                resolution = BrowserInteractionResolutionRepository(
                    connection
                ).for_interaction(previous.id)
                if previous.status is InteractionStatus.BLOCKED and resolution is None:
                    raise ConflictError(
                        "BROWSER_INTERACTION_BLOCKED",
                        "Blocked browser interaction requires explicit operator resolution",
                        evidence={"interaction_id": previous.id},
                    )
                if previous.status is InteractionStatus.BLOCKED and resolution is not None:
                    active_interactions = [
                        item
                        for item in repository.list_for_project(project_id)
                        if item.status
                        in {
                            InteractionStatus.PREPARED,
                            InteractionStatus.SENDING,
                            InteractionStatus.SENT,
                            InteractionStatus.STREAMING,
                            InteractionStatus.CAPTURED,
                        }
                    ]
                    if active_interactions:
                        raise ConflictError(
                            "BROWSER_RECOVERY_REQUIRED",
                            "Another recoverable browser interaction still exists",
                            evidence={
                                "interaction_ids": [
                                    item.id for item in active_interactions
                                ]
                            },
                        )
                    lineage_predecessor = previous
        input_hash = canonical_hash(
            {
                "context_id": context_id,
                "conversation_id": conversation_id,
                "kind": kind.value,
                "operation": "browser.interaction",
                "preparation_id": preparation_id,
                "request_sha256": request_sha256,
                "transport_fingerprint": transport_fingerprint,
            }
        )
        run = stage_run
        if run is None and lineage_predecessor is not None:
            previous_run = StageRunRepository(connection).get(
                lineage_predecessor.stage_run_id
            )
            run = self.run_states.new_run(
                project_id=project_id,
                stage_id=BROWSER_STAGE,
                unit_id="context_load",
                input_hash=input_hash,
                max_attempts=self.config.max_attempts,
                version=previous_run.version + 1,
                supersedes_run_id=previous_run.id,
            )
            StageRunRepository(connection).add(run)
            attempt = 1
            supersedes_interaction_id = lineage_predecessor.id
        if run is None:
            run = self.run_states.new_run(
                project_id=project_id,
                stage_id=BROWSER_STAGE,
                unit_id=("context_load" if unit_id is None else f"unit:{unit_id}"),
                input_hash=input_hash,
                max_attempts=self.config.max_attempts,
                version=attempt + 1,
            )
            StageRunRepository(connection).add(run)
        now = utc_now()
        item = BrowserInteraction(
            id=new_id(),
            project_id=project_id,
            conversation_id=conversation_id,
            context_id=context_id,
            stage_run_id=run.id,
            kind=kind,
            unit_id=unit_id,
            preparation_id=preparation_id,
            status=InteractionStatus.PREPARED,
            attempt=attempt,
            max_attempts=self.config.max_attempts,
            request_artifact_id=request_artifact_id,
            request_sha256=request_sha256,
            transport_fingerprint=transport_fingerprint,
            response_artifact_id=None,
            response_sha256=None,
            capture_method_version=None,
            imported_entity_type=None,
            imported_entity_id=None,
            sent_at=None,
            captured_at=None,
            imported_at=None,
            created_at=now,
            updated_at=now,
            supersedes_interaction_id=supersedes_interaction_id,
        )
        repository.add(item)
        repository.event(
            item.id,
            project_id,
            None,
            item.status,
            (
                {"effect_boundary": "pre_send"}
                if supersedes_interaction_id is None
                else {
                    "effect_boundary": "pre_send",
                    "supersedes_interaction_id": supersedes_interaction_id,
                }
            ),
        )
        return item

    def reprovision_conversation(
        self, connection: sqlite3.Connection, conversation: BrowserConversation
    ) -> BrowserConversation:
        now = utc_now()
        target = replace(
            conversation,
            status=ConversationStatus.PROVISIONING,
            conversation_path=None,
            first_turn_fingerprint=None,
            provisioning_baseline_json=None,
            provisioning_started_at=now,
            ready_at=None,
            updated_at=now,
        )
        BrowserConversationRepository(connection).update(
            target, expected_status=conversation.status
        )
        return target

    def close_provisioning_conversation(
        self, connection: sqlite3.Connection, conversation: BrowserConversation
    ) -> BrowserConversation:
        target = replace(
            conversation,
            status=ConversationStatus.BLOCKED,
            updated_at=utc_now(),
        )
        BrowserConversationRepository(connection).update(
            target, expected_status=conversation.status
        )
        return target

    def begin_send(
        self,
        connection: sqlite3.Connection,
        item: BrowserInteraction,
        *,
        pre_send_turn_anchor: dict[str, object] | None = None,
    ) -> BrowserInteraction:
        predecessor_resolution = (
            None
            if item.supersedes_interaction_id is None
            else BrowserInteractionResolutionRepository(connection).for_interaction(
                item.supersedes_interaction_id
            )
        )
        target = self.interaction_states.transition(
            item,
            InteractionStatus.SENDING,
            attempt_already_counted=(
                predecessor_resolution is not None and item.attempt > 0
            ),
        )
        runs = StageRunRepository(connection)
        run = runs.get(item.stage_run_id)
        running = self.run_states.transition(run, RunStatus.RUNNING)
        runs.update(running, expected_status=run.status)
        BrowserInteractionRepository(connection).update(target, expected_status=item.status)
        evidence: dict[str, object] = {"effect_boundary": "send_attempt_started"}
        if pre_send_turn_anchor is not None:
            evidence["pre_send_turn_anchor"] = pre_send_turn_anchor
        BrowserInteractionRepository(connection).event(
            item.id,
            item.project_id,
            item.status,
            target.status,
            evidence,
        )
        return target

    def record_provisioning_baseline(
        self,
        connection: sqlite3.Connection,
        conversation: BrowserConversation,
        paths: tuple[str, ...],
    ) -> BrowserConversation:
        target = replace(
            conversation,
            provisioning_baseline_json=json.dumps(
                sorted(set(paths)), ensure_ascii=True, separators=(",", ":")
            ),
            updated_at=utc_now(),
        )
        BrowserConversationRepository(connection).update(
            target, expected_status=conversation.status
        )
        return target

    def transition(
        self,
        connection: sqlite3.Connection,
        item: BrowserInteraction,
        target_status: InteractionStatus,
        *,
        evidence: dict[str, object] | None = None,
    ) -> BrowserInteraction:
        if target_status is InteractionStatus.SENT:
            raise ConflictError(
                "BROWSER_SEND_PROOF_REQUIRED",
                "Use the atomic user-turn proof operation to enter sent",
            )
        target = self.interaction_states.transition(item, target_status)
        BrowserInteractionRepository(connection).update(target, expected_status=item.status)
        BrowserInteractionRepository(connection).event(
            item.id, item.project_id, item.status, target.status, evidence
        )
        if target_status is InteractionStatus.IMPORTED:
            self._finish_run(connection, item.stage_run_id)
        elif target_status is InteractionStatus.BLOCKED:
            self._block_run(connection, item.stage_run_id)
        elif target_status is InteractionStatus.FAILED:
            self._fail_run(connection, item.stage_run_id)
        return target

    def resume_blocked_from_proven_user_turn(
        self,
        connection: sqlite3.Connection,
        item: BrowserInteraction,
        *,
        conversation_path: str,
        observed_user_turn_fingerprint: str,
        evidence: dict[str, object],
    ) -> BrowserInteraction:
        if item.status is not InteractionStatus.BLOCKED:
            raise ConflictError(
                "BROWSER_RECOVERY_SOURCE_INVALID",
                "Only a blocked browser interaction can resume from external evidence",
            )
        if not is_real_conversation_path(conversation_path):
            raise ConflictError(
                "BROWSER_CONVERSATION_PATH_INVALID",
                "Recovery proof requires a canonical /c/<uuid> path",
            )
        if observed_user_turn_fingerprint != item.transport_fingerprint:
            raise ConflictError(
                "BROWSER_USER_TURN_FINGERPRINT_MISMATCH",
                "Recovery did not observe the prepared request fingerprint",
            )
        target = self.interaction_states.transition(
            item, InteractionStatus.SENT, recovery=True
        )
        runs = StageRunRepository(connection)
        run = runs.get(item.stage_run_id)
        if run.status is not RunStatus.BLOCKED:
            raise ConflictError(
                "BROWSER_STAGE_RUN_DIVERGENT",
                "Blocked browser interaction does not have a blocked StageRun",
            )
        now = utc_now()
        resumed_run = replace(
            run,
            status=RunStatus.RUNNING,
            finished_at=None,
            updated_at=now,
        )
        runs.update(resumed_run, expected_status=run.status)
        repository = BrowserInteractionRepository(connection)
        repository.update(target, expected_status=item.status)
        repository.event(
            item.id,
            item.project_id,
            item.status,
            target.status,
            {"recovery_reprobe": True, **evidence},
        )
        return target

    def resume_blocked_local_successor_capture(
        self,
        connection: sqlite3.Connection,
        item: BrowserInteraction,
        conversation: BrowserConversation,
        *,
        conversation_path: str,
        selected_user_id: str,
        selected_assistant_id: str,
        assistant_late_bound: bool,
    ) -> BrowserInteraction:
        if item.status is not InteractionStatus.BLOCKED:
            raise ConflictError(
                "BROWSER_RECOVERY_SOURCE_INVALID",
                "Local-successor capture recovery requires a blocked browser interaction",
            )
        if item.kind is not InteractionKind.UNIT_REQUEST:
            raise ConflictError(
                "BROWSER_RECOVERY_KIND_UNSUPPORTED",
                "Local-successor capture recovery requires a unit request",
            )
        if (
            not is_real_conversation_path(conversation_path)
            or conversation.status is not ConversationStatus.READY
            or conversation.conversation_path != conversation_path
            or conversation.context_id != item.context_id
        ):
            raise ConflictError(
                "BROWSER_RECOVERY_WRONG_CONVERSATION",
                "Local-successor capture recovery does not match the proven conversation",
            )
        if not selected_user_id or not selected_assistant_id:
            raise ConflictError(
                "BROWSER_RECOVERY_UNIT_LOCAL_BINDING_INVALID",
                "Local-successor capture recovery requires both bound turn identities",
            )
        target = self.interaction_states.transition(
            item, InteractionStatus.SENT, recovery=True
        )
        runs = StageRunRepository(connection)
        run = runs.get(item.stage_run_id)
        if run.status is not RunStatus.BLOCKED:
            raise ConflictError(
                "BROWSER_STAGE_RUN_DIVERGENT",
                "Blocked local-successor recovery requires a blocked StageRun",
            )
        now = utc_now()
        runs.update(
            replace(
                run,
                status=RunStatus.RUNNING,
                finished_at=None,
                updated_at=now,
            ),
            expected_status=run.status,
        )
        interactions = BrowserInteractionRepository(connection)
        interactions.update(target, expected_status=item.status)
        interactions.event(
            item.id,
            item.project_id,
            item.status,
            target.status,
            {
                "event_type": (
                    "assistant_successor_late_bound"
                    if assistant_late_bound
                    else "local_successor_capture_recovery_resumed"
                ),
                "capture_recovery_resumed": True,
                "reused_send_proof_kind": "post_send_local_successor_v1",
                "external_action_performed": False,
                "conversation_path": conversation_path,
                "selected_user_id": selected_user_id,
                "selected_assistant_id": selected_assistant_id,
                "assistant_identity_state": "resolved",
            },
        )
        return target

    def reconcile_blocked_user_turn_and_bind_conversation(
        self,
        connection: sqlite3.Connection,
        item: BrowserInteraction,
        conversation: BrowserConversation,
        *,
        conversation_path: str,
        observed_user_turn_fingerprint: str,
        sent_at: str,
    ) -> tuple[BrowserInteraction, BrowserConversation]:
        if item.status is not InteractionStatus.BLOCKED:
            raise ConflictError(
                "BROWSER_RECONCILE_SOURCE_INVALID",
                "Reconciliation requires a blocked browser interaction",
            )
        if observed_user_turn_fingerprint != item.transport_fingerprint:
            raise ConflictError(
                "BROWSER_RECONCILE_FINGERPRINT_MISMATCH",
                "Reconciliation did not prove the prepared request fingerprint",
            )
        if not is_real_conversation_path(conversation_path):
            raise ConflictError(
                "BROWSER_CONVERSATION_PATH_INVALID",
                "Reconciliation requires a canonical /c/<uuid> path",
            )
        now = utc_now()
        if conversation.status is ConversationStatus.PROVISIONING:
            if (
                conversation.conversation_path is not None
                or conversation.first_turn_fingerprint is not None
            ):
                raise ConflictError(
                    "BROWSER_CONVERSATION_PROOF_DIVERGENT",
                    "Provisioning conversation already contains unproven send evidence",
                )
            proven_conversation = replace(
                conversation,
                status=ConversationStatus.READY,
                conversation_path=conversation_path,
                first_turn_fingerprint=observed_user_turn_fingerprint,
                ready_at=sent_at,
                updated_at=now,
            )
            BrowserConversationRepository(connection).update(
                proven_conversation, expected_status=conversation.status
            )
        elif conversation.status is ConversationStatus.READY:
            if (
                conversation.conversation_path != conversation_path
                or conversation.first_turn_fingerprint
                != observed_user_turn_fingerprint
            ):
                raise ConflictError(
                    "BROWSER_WRONG_CONVERSATION",
                    "Reconciliation proof belongs to another conversation",
                )
            proven_conversation = conversation
        else:
            raise ConflictError(
                "BROWSER_CONVERSATION_STATE_INVALID",
                "Conversation cannot accept reconciliation evidence in its current state",
            )

        sent = self.interaction_states.transition(
            item, InteractionStatus.SENT, recovery=True
        )
        sent = replace(sent, sent_at=sent_at, updated_at=now)
        runs = StageRunRepository(connection)
        run = runs.get(item.stage_run_id)
        if run.status is not RunStatus.BLOCKED:
            raise ConflictError(
                "BROWSER_STAGE_RUN_DIVERGENT",
                "Blocked interaction reconciliation requires a blocked StageRun",
            )
        resumed_run = replace(
            run,
            status=RunStatus.RUNNING,
            finished_at=None,
            updated_at=now,
        )
        runs.update(resumed_run, expected_status=run.status)
        interactions = BrowserInteractionRepository(connection)
        interactions.update(sent, expected_status=item.status)
        interactions.event(
            item.id,
            item.project_id,
            item.status,
            sent.status,
            {
                "event_type": "explicit_reconciliation",
                "conversation_path": conversation_path,
                "effect_boundary": "send_observed",
                "observed_user_turn_fingerprint": observed_user_turn_fingerprint,
                "sent_at": sent_at,
            },
        )
        return sent, proven_conversation

    def reconcile_blocked_unit_turn(
        self,
        connection: sqlite3.Connection,
        item: BrowserInteraction,
        conversation: BrowserConversation,
        *,
        conversation_path: str,
        user_turn_ordinal: int,
        sent_at: str,
        selected_user_id: str | None = None,
        selected_assistant_id: str | None = None,
    ) -> BrowserInteraction:
        if item.status is not InteractionStatus.BLOCKED:
            raise ConflictError(
                "BROWSER_RECONCILE_SOURCE_INVALID",
                "Unit reconciliation requires a blocked browser interaction",
            )
        if item.kind is not InteractionKind.UNIT_REQUEST:
            raise ConflictError(
                "BROWSER_RECONCILE_KIND_UNSUPPORTED",
                "Structural reconciliation requires a unit request",
            )
        if not is_real_conversation_path(conversation_path):
            raise ConflictError(
                "BROWSER_CONVERSATION_PATH_INVALID",
                "Unit reconciliation requires a canonical conversation path",
            )
        if (
            conversation.status is not ConversationStatus.READY
            or conversation.conversation_path != conversation_path
            or conversation.context_id != item.context_id
            or conversation.first_turn_fingerprint is None
        ):
            raise ConflictError(
                "BROWSER_WRONG_CONVERSATION",
                "Unit reconciliation does not match the bound ready conversation",
            )
        now = utc_now()
        sent = self.interaction_states.transition(
            item, InteractionStatus.SENT, recovery=True
        )
        sent = replace(sent, sent_at=sent_at, updated_at=now)
        runs = StageRunRepository(connection)
        run = runs.get(item.stage_run_id)
        if run.status is not RunStatus.BLOCKED:
            raise ConflictError(
                "BROWSER_STAGE_RUN_DIVERGENT",
                "Blocked unit reconciliation requires a blocked StageRun",
            )
        resumed_run = replace(
            run,
            status=RunStatus.RUNNING,
            finished_at=None,
            updated_at=now,
        )
        runs.update(resumed_run, expected_status=run.status)
        interactions = BrowserInteractionRepository(connection)
        interactions.update(sent, expected_status=item.status)
        interactions.event(
            item.id,
            item.project_id,
            item.status,
            sent.status,
            {
                "event_type": "explicit_reconciliation",
                "proof_kind": "persisted_unit_local_successor_v1",
                "conversation_path": conversation_path,
                "effect_boundary": "send_observed",
                "user_turn_ordinal": user_turn_ordinal,
                "selected_user_id": selected_user_id,
                "selected_assistant_id": selected_assistant_id,
                "transport_fingerprint": item.transport_fingerprint,
                "sent_at": sent_at,
            },
        )
        return sent

    def prove_user_turn_and_bind_conversation(
        self,
        connection: sqlite3.Connection,
        item: BrowserInteraction,
        conversation: BrowserConversation,
        conversation_path: str,
        observed_user_turn_fingerprint: str,
        structural_evidence: dict[str, object] | None = None,
    ) -> tuple[BrowserInteraction, BrowserConversation]:
        if item.status is not InteractionStatus.SENDING:
            raise ConflictError(
                "BROWSER_SEND_PROOF_STATE_INVALID",
                "A user-turn proof can only complete an interaction in sending state",
            )
        if observed_user_turn_fingerprint != item.transport_fingerprint:
            raise ConflictError(
                "BROWSER_USER_TURN_FINGERPRINT_MISMATCH",
                "Observed user turn does not match the prepared request",
            )
        if not is_real_conversation_path(conversation_path):
            raise ConflictError(
                "BROWSER_CONVERSATION_PATH_INVALID",
                "Observed page URL did not contain a canonical /c/<uuid> path",
            )
        now = utc_now()
        if conversation.status is ConversationStatus.PROVISIONING:
            if (
                conversation.conversation_path is not None
                or conversation.first_turn_fingerprint is not None
            ):
                raise ConflictError(
                    "BROWSER_CONVERSATION_PROOF_DIVERGENT",
                    "Provisioning conversation already contains unproven send evidence",
                )
            proven_conversation = replace(
                conversation,
                status=ConversationStatus.READY,
                conversation_path=conversation_path,
                first_turn_fingerprint=observed_user_turn_fingerprint,
                ready_at=now,
                updated_at=now,
            )
            BrowserConversationRepository(connection).update(
                proven_conversation, expected_status=conversation.status
            )
        elif conversation.status is ConversationStatus.READY:
            if conversation.conversation_path != conversation_path:
                raise ConflictError(
                    "BROWSER_WRONG_CONVERSATION",
                    "Observed user turn belongs to another conversation",
                )
            proven_conversation = conversation
        else:
            raise ConflictError(
                "BROWSER_CONVERSATION_STATE_INVALID",
                "Conversation cannot accept a user-turn proof in its current state",
            )
        sent = self.interaction_states.transition(item, InteractionStatus.SENT)
        interactions = BrowserInteractionRepository(connection)
        interactions.update(sent, expected_status=item.status)
        interactions.event(
            item.id,
            item.project_id,
            item.status,
            sent.status,
            {
                "conversation_path": conversation_path,
                "effect_boundary": "send_observed",
                "observed_user_turn_fingerprint": observed_user_turn_fingerprint,
                "user_turn_observed": True,
                **(structural_evidence or {}),
            },
        )
        return sent, proven_conversation

    def register_response(
        self,
        connection: sqlite3.Connection,
        project: Project,
        item: BrowserInteraction,
        stored: StoredFile,
        capture_method_version: str,
    ) -> BrowserInteraction:
        artifact = ArtifactRepository(connection).add_idempotent(
            Artifact(
                id=new_id(),
                project_id=project.id,
                stage_run_id=item.stage_run_id,
                artifact_type="browser_response_raw",
                relative_path=stored.relative_path,
                sha256=stored.sha256,
                byte_size=stored.byte_size,
                version=item.attempt + 1,
                created_at=utc_now(),
            )
        )
        with_evidence = replace(
            item,
            response_artifact_id=artifact.id,
            response_sha256=artifact.sha256,
            capture_method_version=capture_method_version,
        )
        BrowserInteractionRepository(connection).update(with_evidence, expected_status=item.status)
        return BrowserInteractionRepository(connection).get(item.id)

    def set_import_identity(
        self,
        connection: sqlite3.Connection,
        item: BrowserInteraction,
        entity_type: str,
        entity_id: str,
    ) -> BrowserInteraction:
        target = replace(
            item, imported_entity_type=entity_type, imported_entity_id=entity_id
        )
        BrowserInteractionRepository(connection).update(target, expected_status=item.status)
        return BrowserInteractionRepository(connection).get(item.id)

    def record_unit_response_recapture(
        self,
        connection: sqlite3.Connection,
        project: Project,
        item: BrowserInteraction,
        stored: StoredFile,
        *,
        submission_id: str,
        raw_version: int,
        conversation_path: str,
        user_turn_ordinal: int,
    ) -> tuple[BrowserInteraction, Artifact]:
        if item.status not in {
            InteractionStatus.CAPTURED,
            InteractionStatus.IMPORTED,
        }:
            raise ConflictError(
                "BROWSER_RECAPTURE_SOURCE_INVALID",
                "Response recapture requires a captured or imported interaction",
            )
        if (
            item.kind is not InteractionKind.UNIT_REQUEST
            or item.preparation_id is None
            or item.response_artifact_id is None
            or item.response_sha256 is None
            or stored.sha256 == item.response_sha256
        ):
            raise ConflictError(
                "BROWSER_RECAPTURE_BINDING_INVALID",
                "Response recapture bindings or replacement hash are invalid",
            )
        artifacts = ArtifactRepository(connection)
        response_versions = [
            artifact.version
            for artifact in artifacts.list_for_project(project.id)
            if artifact.stage_run_id == item.stage_run_id
            and artifact.artifact_type == "browser_response_raw"
        ]
        artifact = artifacts.add_idempotent(
            Artifact(
                id=new_id(),
                project_id=project.id,
                stage_run_id=item.stage_run_id,
                artifact_type="browser_response_raw",
                relative_path=stored.relative_path,
                sha256=stored.sha256,
                byte_size=stored.byte_size,
                version=(max(response_versions, default=0) + 1),
                created_at=utc_now(),
            )
        )
        now = utc_now()
        evidenced = replace(
            item,
            response_artifact_id=artifact.id,
            response_sha256=artifact.sha256,
            capture_method_version="rendered_text_v1",
            imported_entity_type="text_unit_submission",
            imported_entity_id=submission_id,
            captured_at=now,
            updated_at=now,
        )
        if item.status is InteractionStatus.CAPTURED:
            target = self.interaction_states.transition(
                evidenced, InteractionStatus.IMPORTED
            )
        else:
            run = StageRunRepository(connection).get(item.stage_run_id)
            if run.status is not RunStatus.DONE or run.finished_at is None:
                raise ConflictError(
                    "BROWSER_STAGE_RUN_DIVERGENT",
                    "Imported recapture requires the completed browser StageRun",
                )
            target = replace(evidenced, imported_at=now)
        interactions = BrowserInteractionRepository(connection)
        interactions.update(target, expected_status=item.status)
        interactions.event(
            item.id,
            item.project_id,
            item.status,
            target.status,
            {
                "event_type": "response_recaptured",
                "proof_kind": "persisted_unit_ordinal_v1",
                "conversation_path": conversation_path,
                "user_turn_ordinal": user_turn_ordinal,
                "preparation_id": item.preparation_id,
                "previous_response_artifact_id": item.response_artifact_id,
                "previous_response_sha256": item.response_sha256,
                "response_artifact_id": artifact.id,
                "response_sha256": artifact.sha256,
                "previous_imported_entity_id": item.imported_entity_id,
                "imported_entity_id": submission_id,
                "raw_version": raw_version,
                "capture_method_version": "rendered_text_v1",
            },
        )
        if item.status is InteractionStatus.CAPTURED:
            self._finish_run(connection, item.stage_run_id)
        return BrowserInteractionRepository(connection).get(item.id), artifact

    def _finish_run(self, connection: sqlite3.Connection, run_id: str) -> None:
        repository = StageRunRepository(connection)
        run = repository.get(run_id)
        if run.status is RunStatus.DONE:
            return
        target = self.run_states.transition(run, RunStatus.DONE)
        repository.update(target, expected_status=run.status)

    def _fail_run(self, connection: sqlite3.Connection, run_id: str) -> None:
        repository = StageRunRepository(connection)
        run = repository.get(run_id)
        if run.status is RunStatus.PENDING:
            running = self.run_states.transition(run, RunStatus.RUNNING)
            repository.update(running, expected_status=run.status)
            run = running
        if run.status is RunStatus.RUNNING:
            failed = self.run_states.transition(run, RunStatus.FAILED)
            repository.update(failed, expected_status=run.status)

    def _block_run(self, connection: sqlite3.Connection, run_id: str) -> None:
        repository = StageRunRepository(connection)
        run = repository.get(run_id)
        if run.status is RunStatus.PENDING:
            blocked = self.run_states.transition(run, RunStatus.BLOCKED)
            repository.update(blocked, expected_status=run.status)
            return
        if run.status is RunStatus.RUNNING:
            failed = self.run_states.transition(run, RunStatus.FAILED)
            repository.update(failed, expected_status=run.status)
            run = failed
        if run.status is RunStatus.FAILED:
            blocked = self.run_states.transition(run, RunStatus.BLOCKED)
            repository.update(blocked, expected_status=run.status)

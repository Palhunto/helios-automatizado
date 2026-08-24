from __future__ import annotations

import json
import sqlite3
from dataclasses import astuple

from ebook_pipeline.browser.models import (
    BrowserConversation,
    BrowserConversationInvalidation,
    BrowserInteraction,
    BrowserInteractionResolution,
    ConversationResolutionAction,
    ConversationStatus,
    InteractionKind,
    InteractionResolutionKind,
    InteractionStatus,
)
from ebook_pipeline.browser.urls import is_real_conversation_path
from ebook_pipeline.core.errors import ConflictError, NotFoundError
from ebook_pipeline.core.ids import new_id, utc_now


def _conversation(row: sqlite3.Row) -> BrowserConversation:
    values = dict(row)
    values["status"] = ConversationStatus(values["status"])
    return BrowserConversation(**values)


def _interaction(row: sqlite3.Row) -> BrowserInteraction:
    values = dict(row)
    values["kind"] = InteractionKind(values["kind"])
    values["status"] = InteractionStatus(values["status"])
    return BrowserInteraction(**values)


def _resolution(row: sqlite3.Row) -> BrowserInteractionResolution:
    values = dict(row)
    values["resolution"] = InteractionResolutionKind(values["resolution"])
    values["conversation_action"] = ConversationResolutionAction(
        values["conversation_action"]
    )
    return BrowserInteractionResolution(**values)


def _context_lineage_is_operator_abandoned(
    connection: sqlite3.Connection, conversation: BrowserConversation
) -> bool:
    if (
        conversation.conversation_path is not None
        and not is_real_conversation_path(conversation.conversation_path)
    ):
        return False
    active = connection.execute(
        "SELECT 1 FROM browser_interactions WHERE conversation_id = ? "
        "AND status IN ('prepared', 'sending', 'sent', 'streaming', 'captured') LIMIT 1",
        (conversation.id,),
    ).fetchone()
    if active is not None:
        return False
    latest = connection.execute(
        "SELECT i.id, i.status FROM browser_interactions i "
        "WHERE i.conversation_id = ? AND i.kind = 'context_load' "
        "ORDER BY i.created_at DESC, i.id DESC LIMIT 1",
        (conversation.id,),
    ).fetchone()
    if latest is None or latest["status"] != InteractionStatus.BLOCKED.value:
        return False
    resolution = connection.execute(
        "SELECT resolution FROM browser_interaction_resolutions WHERE interaction_id = ?",
        (latest["id"],),
    ).fetchone()
    return (
        resolution is not None
        and resolution["resolution"] == InteractionResolutionKind.OPERATOR_ABANDONED.value
    )


class BrowserConversationRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def add(self, item: BrowserConversation) -> None:
        self.connection.execute(
            "INSERT INTO browser_conversations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            astuple(item),
        )

    def get(self, conversation_id: str) -> BrowserConversation:
        row = self.connection.execute(
            "SELECT * FROM browser_conversations WHERE id = ?", (conversation_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("BROWSER_CONVERSATION_NOT_FOUND", "Conversation was not found")
        return _conversation(row)

    def for_context(self, context_id: str) -> BrowserConversation | None:
        rows = self.connection.execute(
            "SELECT c.* FROM browser_conversations c "
            "LEFT JOIN browser_conversation_invalidations i ON i.conversation_id = c.id "
            "WHERE c.context_id = ? AND i.id IS NULL ORDER BY c.created_at DESC, c.id DESC",
            (context_id,),
        ).fetchall()
        conversations: list[BrowserConversation] = []
        for row in rows:
            item = _conversation(row)
            if not _context_lineage_is_operator_abandoned(self.connection, item):
                conversations.append(item)
        if len(conversations) > 1:
            raise ConflictError(
                "BROWSER_ACTIVE_CONVERSATION_AMBIGUOUS",
                "Context has more than one non-invalidated browser conversation",
            )
        return None if not conversations else conversations[0]

    def list_for_context(self, context_id: str) -> list[BrowserConversation]:
        rows = self.connection.execute(
            "SELECT * FROM browser_conversations WHERE context_id = ? "
            "ORDER BY created_at, id",
            (context_id,),
        ).fetchall()
        return [_conversation(row) for row in rows]

    def update(
        self, item: BrowserConversation, *, expected_status: ConversationStatus
    ) -> None:
        cursor = self.connection.execute(
            "UPDATE browser_conversations SET status = ?, conversation_path = ?, "
            "first_turn_fingerprint = ?, provisioning_baseline_json = ?, "
            "provisioning_started_at = ?, ready_at = ?, updated_at = ? "
            "WHERE id = ? AND status = ?",
            (
                item.status,
                item.conversation_path,
                item.first_turn_fingerprint,
                item.provisioning_baseline_json,
                item.provisioning_started_at,
                item.ready_at,
                item.updated_at,
                item.id,
                expected_status,
            ),
        )
        if cursor.rowcount != 1:
            raise ConflictError(
                "BROWSER_CONVERSATION_CONCURRENT_UPDATE",
                "Conversation state changed concurrently",
            )


class BrowserInteractionRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def add(self, item: BrowserInteraction) -> None:
        self.connection.execute(
            "INSERT INTO browser_interactions VALUES ("
            + ", ".join("?" for _ in astuple(item))
            + ")",
            astuple(item),
        )

    def get(self, interaction_id: str) -> BrowserInteraction:
        row = self.connection.execute(
            "SELECT * FROM browser_interactions WHERE id = ?", (interaction_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("BROWSER_INTERACTION_NOT_FOUND", "Interaction was not found")
        return _interaction(row)

    def by_request_attempt(
        self, conversation_id: str, request_sha256: str, attempt: int
    ) -> BrowserInteraction | None:
        row = self.connection.execute(
            "SELECT * FROM browser_interactions WHERE conversation_id = ? "
            "AND request_sha256 = ? AND attempt = ?",
            (conversation_id, request_sha256, attempt),
        ).fetchone()
        return None if row is None else _interaction(row)

    def by_request(
        self, conversation_id: str, request_sha256: str
    ) -> BrowserInteraction | None:
        row = self.connection.execute(
            "SELECT * FROM browser_interactions WHERE conversation_id = ? "
            "AND request_sha256 = ? ORDER BY created_at DESC LIMIT 1",
            (conversation_id, request_sha256),
        ).fetchone()
        return None if row is None else _interaction(row)

    def latest_for_context_kind(
        self, context_id: str, kind: InteractionKind, unit_id: str | None = None
    ) -> BrowserInteraction | None:
        row = self.connection.execute(
            "SELECT * FROM browser_interactions WHERE context_id = ? AND kind = ? "
            "AND unit_id IS ? ORDER BY created_at DESC LIMIT 1",
            (context_id, kind, unit_id),
        ).fetchone()
        return None if row is None else _interaction(row)

    def list_incomplete(self, project_id: str) -> list[BrowserInteraction]:
        rows = self.connection.execute(
            "SELECT * FROM browser_interactions WHERE project_id = ? "
            "AND status NOT IN ('imported', 'failed', 'blocked') ORDER BY created_at",
            (project_id,),
        ).fetchall()
        return [_interaction(row) for row in rows]

    def list_for_conversation(self, conversation_id: str) -> list[BrowserInteraction]:
        rows = self.connection.execute(
            "SELECT * FROM browser_interactions WHERE conversation_id = ? "
            "ORDER BY created_at, id",
            (conversation_id,),
        ).fetchall()
        return [_interaction(row) for row in rows]

    def list_for_project(self, project_id: str) -> list[BrowserInteraction]:
        rows = self.connection.execute(
            "SELECT * FROM browser_interactions WHERE project_id = ? ORDER BY created_at, id",
            (project_id,),
        ).fetchall()
        return [_interaction(row) for row in rows]

    def list_blocked(self, project_id: str) -> list[BrowserInteraction]:
        rows = self.connection.execute(
            "SELECT * FROM browser_interactions WHERE project_id = ? AND status = 'blocked' "
            "ORDER BY created_at, id",
            (project_id,),
        ).fetchall()
        return [_interaction(row) for row in rows]

    def events(self, interaction_id: str) -> list[dict[str, object]]:
        rows = self.connection.execute(
            "SELECT id, previous_status, status, evidence_json, created_at "
            "FROM browser_interaction_events WHERE interaction_id = ? "
            "ORDER BY created_at, id",
            (interaction_id,),
        ).fetchall()
        result: list[dict[str, object]] = []
        for row in rows:
            evidence_json = row["evidence_json"]
            result.append(
                {
                    "id": str(row["id"]),
                    "previous_status": row["previous_status"],
                    "status": str(row["status"]),
                    "evidence": (
                        None if evidence_json is None else json.loads(str(evidence_json))
                    ),
                    "created_at": str(row["created_at"]),
                }
            )
        return result

    def update(
        self, item: BrowserInteraction, *, expected_status: InteractionStatus
    ) -> None:
        cursor = self.connection.execute(
            "UPDATE browser_interactions SET status = ?, attempt = ?, response_artifact_id = ?, "
            "response_sha256 = ?, capture_method_version = ?, imported_entity_type = ?, "
            "imported_entity_id = ?, sent_at = ?, captured_at = ?, imported_at = ?, "
            "updated_at = ? WHERE id = ? AND status = ?",
            (
                item.status,
                item.attempt,
                item.response_artifact_id,
                item.response_sha256,
                item.capture_method_version,
                item.imported_entity_type,
                item.imported_entity_id,
                item.sent_at,
                item.captured_at,
                item.imported_at,
                item.updated_at,
                item.id,
                expected_status,
            ),
        )
        if cursor.rowcount != 1:
            raise ConflictError(
                "BROWSER_INTERACTION_CONCURRENT_UPDATE",
                "Browser interaction state changed concurrently",
            )

    def event(
        self,
        interaction_id: str,
        project_id: str,
        previous_status: InteractionStatus | None,
        status: InteractionStatus,
        evidence: dict[str, object] | None = None,
    ) -> None:
        self.connection.execute(
            "INSERT INTO browser_interaction_events "
            "(id, interaction_id, project_id, previous_status, status, evidence_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                new_id(),
                interaction_id,
                project_id,
                None if previous_status is None else previous_status.value,
                status.value,
                None
                if evidence is None
                else json.dumps(
                    evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ),
                utc_now(),
            ),
        )


class BrowserInteractionResolutionRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def add(self, item: BrowserInteractionResolution) -> None:
        try:
            self.connection.execute(
                "INSERT INTO browser_interaction_resolutions "
                "(id, interaction_id, project_id, resolution, operator, reason, "
                "conversation_action, evidence_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.id,
                    item.interaction_id,
                    item.project_id,
                    item.resolution.value,
                    item.operator,
                    item.reason,
                    item.conversation_action.value,
                    item.evidence_json,
                    item.created_at,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(
                "BROWSER_INTERACTION_RESOLUTION_CONFLICT",
                "Browser interaction already has an operator resolution",
            ) from exc

    def for_interaction(self, interaction_id: str) -> BrowserInteractionResolution | None:
        row = self.connection.execute(
            "SELECT * FROM browser_interaction_resolutions WHERE interaction_id = ?",
            (interaction_id,),
        ).fetchone()
        return None if row is None else _resolution(row)


class BrowserConversationInvalidationRepository:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def add(self, item: BrowserConversationInvalidation) -> None:
        try:
            self.connection.execute(
                "INSERT INTO browser_conversation_invalidations "
                "(id, conversation_id, project_id, replacement_conversation_id, reason, "
                "evidence_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                astuple(item),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError(
                "BROWSER_CONVERSATION_INVALIDATION_CONFLICT",
                "Browser conversation already has an invalidation decision",
            ) from exc

    def for_conversation(
        self, conversation_id: str
    ) -> BrowserConversationInvalidation | None:
        row = self.connection.execute(
            "SELECT * FROM browser_conversation_invalidations WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
        return None if row is None else BrowserConversationInvalidation(**dict(row))

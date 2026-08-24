from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    PENDING_RETRY = "pending_retry"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class Project:
    id: str
    name: str
    slug: str
    config_path: str
    artifact_root: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class StageRun:
    id: str
    project_id: str
    stage_id: str
    unit_id: str
    status: RunStatus
    input_hash: str
    idempotency_key: str
    version: int
    attempt: int
    max_attempts: int
    started_at: str | None
    finished_at: str | None
    created_at: str
    updated_at: str
    supersedes_run_id: str | None = None


@dataclass(frozen=True, slots=True)
class Artifact:
    id: str
    project_id: str
    stage_run_id: str
    artifact_type: str
    relative_path: str
    sha256: str
    byte_size: int
    version: int
    created_at: str


@dataclass(frozen=True, slots=True)
class ErrorRecord:
    id: str
    project_id: str
    stage_run_id: str
    code: str
    message: str
    recoverable: bool
    evidence_json: str | None
    created_at: str
    resolved_at: str | None = None
    resolution: str | None = None


@dataclass(frozen=True, slots=True)
class StoredFile:
    relative_path: str
    sha256: str
    byte_size: int
    already_existed: bool


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    code: str
    message: str
    relative_path: str | None = None


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    stage_run_id: str
    previous_status: RunStatus
    status: RunStatus
    message: str

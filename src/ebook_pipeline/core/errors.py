from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ErrorContext:
    evidence: dict[str, Any] | None = None
    next_action: str | None = None


class HeliosError(Exception):
    exit_code = 1

    def __init__(
        self,
        code: str,
        message: str,
        *,
        recoverable: bool = False,
        evidence: dict[str, Any] | None = None,
        next_action: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.recoverable = recoverable
        self.context = ErrorContext(evidence=evidence, next_action=next_action)


class ConfigurationError(HeliosError):
    exit_code = 2


class AcademicValidationError(HeliosError):
    exit_code = 2


class WritingValidationError(HeliosError):
    exit_code = 2


class ConflictError(HeliosError):
    exit_code = 3


class NotFoundError(HeliosError):
    exit_code = 4


class IntegrityError(HeliosError):
    exit_code = 5


class MigrationError(IntegrityError):
    pass


class StateTransitionError(ConflictError):
    pass


class ArtifactError(IntegrityError):
    pass

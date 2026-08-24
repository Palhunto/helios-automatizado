from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

CONTEXT_FIELDS = (
    "project_id",
    "stage_id",
    "unit_id",
    "operation",
    "status",
    "error_code",
)
SECRET_PATTERN = re.compile(
    r"(?i)\b(password|token|secret|cookie|authorization|api[_-]?key)\b\s*[:=]\s*([^\s,;]+)"
)


def redact(value: str) -> str:
    return SECRET_PATTERN.sub(lambda match: f"{match.group(1)}=[REDACTED]", value)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "message": redact(record.getMessage()),
        }
        for field in CONTEXT_FIELDS:
            payload[field] = getattr(record, field, None)
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)


class ContextDefaults(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        for field in CONTEXT_FIELDS:
            if not hasattr(record, field):
                setattr(record, field, None)
        return True


def configure_logging(level: str, *, project_log: Path | None = None) -> logging.Logger:
    logger = logging.getLogger("ebook_pipeline")
    for handler in logger.handlers:
        handler.close()
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(level)

    console = logging.StreamHandler()
    console.addFilter(ContextDefaults())
    console.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
    logger.addHandler(console)

    if project_log is not None:
        project_log.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            project_log, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        file_handler.addFilter(ContextDefaults())
        file_handler.setFormatter(JsonFormatter())
        logger.addHandler(file_handler)
    return logger


def log_event(
    logger: logging.Logger,
    level: int,
    message: str,
    *,
    project_id: str | None = None,
    stage_id: str | None = None,
    unit_id: str | None = None,
    operation: str | None = None,
    status: str | None = None,
    error_code: str | None = None,
) -> None:
    logger.log(
        level,
        message,
        extra={
            "project_id": project_id,
            "stage_id": stage_id,
            "unit_id": unit_id,
            "operation": operation,
            "status": status,
            "error_code": error_code,
        },
    )

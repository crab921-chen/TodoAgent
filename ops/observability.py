"""Structured, request-correlated logging with secret redaction."""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from harness.hooks import RuntimeEvent


_SECRET_PATTERNS = (
    (
        re.compile(
            r"(?i)([\"']?(?:api[_-]?key|authorization|x-api-key|groq_api_key|zhipu_api_key)[\"']?\s*[:=]\s*)(?:[\"']?(?:(?:bearer|token|basic)\s+[^,\s}\"']+|[^,\s}\"']+)[\"']?)"
        ),
        r"\1<redacted>",
    ),
    (
        re.compile(r"(?i)\b(?:authorization|api[_-]?key|x-api-key|groq_api_key|zhipu_api_key)\b\s*[:=]\s*(?:(?:bearer|token|basic)\s+)?[^\s,;\"'}]+"),
        "<redacted>",
    ),
    (
        re.compile(r"(?i)\b(?:bearer|token|basic)\s+[A-Za-z0-9._~+/-]{8,}"),
        "<redacted-authorization>",
    ),
    (re.compile(r"\bgsk_[A-Za-z0-9_-]{12,}\b"), "<redacted-api-key>"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"), "<redacted-api-key>"),
)


def redact_text(value: str) -> str:
    """Remove common API-key and authorization-token forms from text."""

    result = str(value)
    for pattern, replacement in _SECRET_PATTERNS:
        result = pattern.sub(replacement, result)
    return result


def redact_value(value: Any) -> Any:
    """Redact strings recursively while bounding structured log values."""

    if isinstance(value, str):
        return redact_text(value)[:2000]
    if isinstance(value, dict):
        return {str(key): redact_value(item) for key, item in list(value.items())[:32]}
    if isinstance(value, (list, tuple)):
        return [redact_value(item) for item in list(value)[:32]]
    return value


class JsonFormatter(logging.Formatter):
    """Emit one bounded JSON object per log record."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": redact_text(record.getMessage()),
        }
        for key in ("request_id", "user_id", "event", "status", "error_code"):
            if hasattr(record, key):
                payload[key] = redact_value(getattr(record, key))
        if record.exc_info:
            payload["exception"] = redact_text(self.formatException(record.exc_info))[:4000]
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


class RuntimeEventLogger:
    """Log only bounded runtime event metadata, never raw prompts or arguments."""

    def __init__(self, logger: logging.Logger | None = None) -> None:
        self.logger = logger or logging.getLogger("todo.runtime")

    def emit(self, event: RuntimeEvent) -> None:
        self.logger.info(
            "runtime_event",
            extra={
                "event": f"{event.kind}.{event.phase}",
                "request_id": event.request_id,
                "user_id": event.user_id,
                "status": "ok" if event.ok is not False else "failed",
                "error_code": event.error_code,
            },
        )


def configure_logging(level: str = "INFO") -> None:
    """Configure a single redacting JSON stream handler for deployment."""

    root = logging.getLogger()
    root.setLevel(level.upper())
    if not any(isinstance(handler, _RedactingHandler) for handler in root.handlers):
        handler = _RedactingHandler()
        handler.setFormatter(JsonFormatter())
        root.addHandler(handler)


class _RedactingHandler(logging.StreamHandler):
    """Stream handler marker used to keep configuration idempotent."""

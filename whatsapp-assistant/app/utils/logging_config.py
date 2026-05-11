"""JSON structured logging for the assistant.

One JSON object per line, written to stdout. ``configure_logging`` replaces all
root handlers so basic ``logging.info(...)`` calls anywhere in the app produce
structured records.

Sensitive fields (anything that looks like a token / secret / authorization
header) are redacted before emission.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any

_SENSITIVE_KEY_PATTERN = re.compile(
    r"(token|secret|password|api_key|authorization|access_token|refresh_token)",
    re.IGNORECASE,
)
_BEARER_PATTERN = re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]+", re.IGNORECASE)
_REDACTED = "[REDACTED]"

_RESERVED_LOG_RECORD_ATTRS = {
    "name",
    "msg",
    "args",
    "levelname",
    "levelno",
    "pathname",
    "filename",
    "module",
    "exc_info",
    "exc_text",
    "stack_info",
    "lineno",
    "funcName",
    "created",
    "msecs",
    "relativeCreated",
    "thread",
    "threadName",
    "processName",
    "process",
    "message",
    "asctime",
    "taskName",
}


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return _BEARER_PATTERN.sub(rf"\1{_REDACTED}", value)
    if isinstance(value, dict):
        return _redact_mapping(value)
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    return value


def _redact_mapping(payload: dict[str, Any]) -> dict[str, Any]:
    redacted: dict[str, Any] = {}
    for key, value in payload.items():
        if isinstance(key, str) and _SENSITIVE_KEY_PATTERN.search(key):
            redacted[key] = _REDACTED
        else:
            redacted[key] = _redact_value(value)
    return redacted


class JsonFormatter(logging.Formatter):
    """Render ``LogRecord``s as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": _redact_value(record.getMessage()),
        }
        for attr, value in record.__dict__.items():
            if attr in _RESERVED_LOG_RECORD_ATTRS or attr.startswith("_"):
                continue
            payload[attr] = _redact_value(value)

        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)

        payload = _redact_mapping(payload)
        return json.dumps(payload, default=str)


def configure_logging(level: int = logging.INFO) -> None:
    """Install the JSON formatter on the root logger.

    Idempotent — calling it twice replaces the previous handlers cleanly.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level)

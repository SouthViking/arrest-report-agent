"""Small structured logging setup, matching RoomGate's log format."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any, Literal

STRUCTURED_EVENT_ATTRIBUTE = "structured_event"


class JsonLogFormatter(logging.Formatter):
    """Render application and dependency logs as valid JSON objects."""

    def __init__(self, *, indented: bool = True) -> None:
        super().__init__()
        self.indented = indented

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
        }
        structured_event = getattr(record, STRUCTURED_EVENT_ATTRIBUTE, None)
        if isinstance(structured_event, dict):
            payload.update(structured_event)
        else:
            payload["message"] = record.getMessage()
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        elif record.exc_text:
            payload["exception"] = record.exc_text
        return json.dumps(
            payload,
            ensure_ascii=False,
            indent=2 if self.indented else None,
            separators=None if self.indented else (",", ":"),
        )


def configure_logging(level: str, *, json_indented: bool = True) -> None:
    """Configure process-wide JSON logs with optional human-readable indentation."""

    handler = logging.StreamHandler()
    handler.setFormatter(JsonLogFormatter(indented=json_indented))
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        handlers=[handler],
        force=True,
    )


def log_event(
    logger: logging.Logger,
    level: Literal["debug", "info", "warning", "error", "critical"],
    event: str,
    exc_info: BaseException | bool | None = None,
    **fields: Any,
) -> None:
    """Write a stable event and optionally attach a complete exception traceback."""

    structured_event = {"event": event}
    structured_event.update({key: _safe_value(value) for key, value in sorted(fields.items())})
    getattr(logger, level)(
        event,
        exc_info=exc_info,
        extra={STRUCTURED_EVENT_ATTRIBUTE: structured_event},
    )


def exception_fields(error: BaseException) -> dict[str, Any]:
    """Extract compact diagnostics while leaving the full traceback to ``exc_info``."""

    fields: dict[str, Any] = {
        "error_type": type(error).__name__,
        "error_message": str(error) or repr(error),
    }
    for attribute in ("status_code", "code", "request_id"):
        value = getattr(error, attribute, None)
        if value is not None:
            fields[f"error_{attribute}"] = value
    return fields


def _safe_value(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    text = str(value).replace("\n", " ").replace("\r", " ")
    return text[:240]

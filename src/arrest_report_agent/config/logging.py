"""Small structured logging setup, matching RoomGate's log format."""

from __future__ import annotations

import logging
from typing import Any, Literal


def configure_logging(level: str) -> None:
    """Configure process-wide logs using RoomGate's compact format."""

    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def log_event(
    logger: logging.Logger,
    level: Literal["debug", "info", "warning", "error", "critical"],
    event: str,
    exc_info: BaseException | bool | None = None,
    **fields: Any,
) -> None:
    """Write a stable event and optionally attach a complete exception traceback."""

    rendered = " ".join(f"{key}={_safe_value(value)}" for key, value in sorted(fields.items()))
    message = f"event={event}"
    if rendered:
        message = f"{message} {rendered}"
    getattr(logger, level)(message, exc_info=exc_info)


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


def _safe_value(value: Any) -> str:
    text = str(value).replace("\n", " ").replace("\r", " ")
    return text[:240]

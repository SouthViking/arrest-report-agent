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
    **fields: Any,
) -> None:
    """Write a stable event plus non-sensitive key/value fields."""

    rendered = " ".join(f"{key}={_safe_value(value)}" for key, value in sorted(fields.items()))
    message = f"event={event}"
    if rendered:
        message = f"{message} {rendered}"
    getattr(logger, level)(message)


def _safe_value(value: Any) -> str:
    text = str(value).replace("\n", " ").replace("\r", " ")
    return text[:240]


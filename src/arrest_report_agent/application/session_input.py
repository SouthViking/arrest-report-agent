"""Keep transient media out of durable agent conversations."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, cast

from agents.items import TResponseInputItem

HISTORICAL_IMAGE_PLACEHOLDER = (
    "[Imagen histórica omitida. Si aún necesitas verla, vuelve a cargarla con la herramienta "
    "correspondiente.]"
)
HISTORICAL_FILE_PLACEHOLDER = (
    "[Archivo histórico omitido. Si aún necesitas leerlo, vuelve a cargarlo con la herramienta "
    "correspondiente.]"
)


def sanitize_session_items(items: list[TResponseInputItem]) -> list[TResponseInputItem]:
    """Replace persisted image/file payloads while preserving item and tool-call structure."""

    return [cast(TResponseInputItem, _sanitize_value(deepcopy(item))) for item in items]


def merge_sanitized_session_input(
    history_items: list[TResponseInputItem],
    new_items: list[TResponseInputItem],
) -> list[TResponseInputItem]:
    """Strip stale media only from history and keep the current turn intact."""

    return [*sanitize_session_items(history_items), *new_items]


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, list):
        return [_sanitize_value(item) for item in value]
    if not isinstance(value, dict):
        return value

    item_type = value.get("type")
    if item_type in {"input_image", "output_image"}:
        return {"type": "input_text", "text": HISTORICAL_IMAGE_PLACEHOLDER}
    if item_type in {"input_file", "output_file"}:
        return {"type": "input_text", "text": HISTORICAL_FILE_PLACEHOLDER}
    return {key: _sanitize_value(item) for key, item in value.items()}

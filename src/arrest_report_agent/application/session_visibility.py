"""Select report-scoped staff tool results for a report owner's session view."""

from __future__ import annotations

import json
from typing import Any, cast

from agents.items import TResponseInputItem

from .member_names import normalize_person_name

_PUBLIC_REFERENCE_TOOLS = frozenset(
    {"get_report_guide", "search_radio_codes", "list_charge_index", "get_charge_details"}
)


def owner_visible_session_items(
    stored: list[tuple[str, TResponseInputItem]],
    *,
    owner_session_id: str,
    suspect_name: str | None,
    report_id: int | None,
    parent_channel_id: int,
    root_message_id: int,
    thread_id: int | None,
) -> list[TResponseInputItem]:
    """Keep the owner's own history and only verifiably in-scope staff tool pairs.

    Staff prompts, draft responses and other-channel tool results are not necessarily
    published Discord messages. Public thread messages arrive through Discord context.
    """

    allowed_calls: set[str] = set()
    for session_id, item in stored:
        if session_id == owner_session_id or not isinstance(item, dict):
            continue
        data = cast(dict[str, Any], item)
        if data.get("type") != "function_call":
            continue
        call_id = data.get("call_id")
        if isinstance(call_id, str) and _report_scoped_call(
            data,
            suspect_name=suspect_name,
            report_id=report_id,
            parent_channel_id=parent_channel_id,
            root_message_id=root_message_id,
            thread_id=thread_id,
        ):
            allowed_calls.add(call_id)

    visible: list[TResponseInputItem] = []
    for session_id, item in stored:
        if session_id == owner_session_id:
            visible.append(item)
        elif isinstance(item, dict):
            data = cast(dict[str, Any], item)
            if (
                isinstance(data.get("call_id"), str)
                and data["call_id"] in allowed_calls
                and data.get("type") in {"function_call", "function_call_output"}
            ):
                visible.append(item)
    return visible


def _report_scoped_call(
    item: dict[str, Any],
    *,
    suspect_name: str | None,
    report_id: int | None,
    parent_channel_id: int,
    root_message_id: int,
    thread_id: int | None,
) -> bool:
    name = item.get("name")
    if name in _PUBLIC_REFERENCE_TOOLS or name == "get_current_arrest_report":
        return True
    arguments = item.get("arguments")
    try:
        args = json.loads(arguments) if isinstance(arguments, str) else arguments
    except json.JSONDecodeError:
        return False
    if not isinstance(args, dict):
        return False

    if name == "get_character_legal_facts":
        character = args.get("character_name")
        return (
            suspect_name is not None
            and report_id is not None
            and isinstance(character, str)
            and normalize_person_name(character) == normalize_person_name(suspect_name)
            and _same_id(args.get("exclude_arrest_id"), report_id)
        )
    if name in {"get_arrest_report", "get_report_suspect_images", "get_report_supporting_images"}:
        requested_id = args.get("report_id")
        return report_id is not None and (
            (requested_id is None and name != "get_arrest_report")
            or _same_id(requested_id, report_id)
        )
    if name == "get_report_officer_rank":
        return args.get("names") is None
    if name in {"get_messages", "get_message_attachments"}:
        channel_id = args.get("channel_id")
        if channel_id is None:
            channel_id = thread_id or parent_channel_id
        if thread_id is not None and _same_id(channel_id, thread_id):
            return True
        return (
            _same_id(channel_id, parent_channel_id)
            and _same_id(args.get("message_id"), root_message_id)
            and (name != "get_messages" or args.get("direction", "exact") == "exact")
        )
    return False


def _same_id(value: object, expected: int) -> bool:
    return isinstance(value, (str, int)) and str(value) == str(expected)

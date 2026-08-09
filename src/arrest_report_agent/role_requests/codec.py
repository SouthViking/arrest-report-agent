"""Versioned Discord embed codec used as the role-request source of truth."""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import UTC, datetime

import discord

from arrest_report_agent.config import PersonaRoleRequestMessages

from .models import RoleRequest, RoleRequestDecision, RoleRequestStatus

CODEC_VERSION = "role-request:v1"
APPROVE_ALL_CUSTOM_ID = f"{CODEC_VERSION}:approve-all"
APPROVE_PARTIAL_CUSTOM_ID = f"{CODEC_VERSION}:approve-partial"
REJECT_CUSTOM_ID = f"{CODEC_VERSION}:reject"
DECISION_CUSTOM_IDS = frozenset(
    {APPROVE_ALL_CUSTOM_ID, APPROVE_PARTIAL_CUSTOM_ID, REJECT_CUSTOM_ID}
)
EMPTY_VALUE = "—"
FOOTER_PATTERN = re.compile(r"^Solicitud (?P<message_id>[1-9][0-9]*) · role-request:v1$")
USER_URL_PATTERN = re.compile(r"^https://discord\.com/users/(?P<user_id>[1-9][0-9]*)$")
ROLE_MENTION_PATTERN = re.compile(r"^<@&(?P<role_id>[1-9][0-9]*)>$")


class RoleRequestCodecError(ValueError):
    """Raised when a Discord message is not a trustworthy role request."""


def build_pending_embed(
    *,
    requester: discord.Member,
    add_role_ids: Iterable[int],
    remove_role_ids: Iterable[int],
    reason: str | None,
    ui: PersonaRoleRequestMessages,
    display_name: str,
    created_at: datetime,
    message_id: int | None = None,
) -> discord.Embed:
    """Build the polished pending request; the draft intentionally has no valid footer."""

    embed = discord.Embed(
        title=ui.pending_title,
        description=ui.pending_description.format(display_name=display_name),
        color=discord.Color.gold(),
        timestamp=created_at,
    )
    avatar = getattr(getattr(requester, "display_avatar", None), "url", None)
    embed.set_author(
        name=requester.display_name,
        url=f"https://discord.com/users/{requester.id}",
        icon_url=str(avatar) if avatar else None,
    )
    embed.add_field(
        name=ui.add_field_title,
        value=_render_role_mentions(add_role_ids, EMPTY_VALUE),
        inline=True,
    )
    embed.add_field(
        name=ui.remove_field_title,
        value=_render_role_mentions(remove_role_ids, EMPTY_VALUE),
        inline=True,
    )
    embed.add_field(
        name=ui.reason_field_title,
        value=reason or EMPTY_VALUE,
        inline=False,
    )
    embed.add_field(
        name=ui.status_field_title,
        value=ui.status_pending,
        inline=False,
    )
    if message_id is not None:
        embed.set_footer(text=f"Solicitud {message_id} · {CODEC_VERSION}")
    return embed


def build_decision_embed(
    request: RoleRequest,
    decision: RoleRequestDecision,
    *,
    requester_name: str,
    requester_avatar_url: str | None,
    ui: PersonaRoleRequestMessages,
    display_name: str,
) -> discord.Embed:
    """Render one terminal, human-readable decision while preserving the request fields."""

    title, status, color = _decision_presentation(decision.status, ui)
    embed = discord.Embed(
        title=title,
        description=ui.pending_description.format(display_name=display_name),
        color=color,
        timestamp=request.created_at,
    )
    embed.set_author(
        name=requester_name,
        url=f"https://discord.com/users/{request.requester_id}",
        icon_url=requester_avatar_url,
    )
    embed.add_field(
        name=ui.add_field_title,
        value=_render_role_mentions(request.add_role_ids, EMPTY_VALUE),
        inline=True,
    )
    embed.add_field(
        name=ui.remove_field_title,
        value=_render_role_mentions(request.remove_role_ids, EMPTY_VALUE),
        inline=True,
    )
    embed.add_field(
        name=ui.reason_field_title,
        value=request.reason or EMPTY_VALUE,
        inline=False,
    )
    embed.add_field(name=ui.status_field_title, value=status, inline=False)

    if decision.status in {
        RoleRequestStatus.APPROVED,
        RoleRequestStatus.PARTIALLY_APPROVED,
    }:
        embed.add_field(
            name=ui.approved_changes_title,
            value=_render_changes(
                decision.approved_add_role_ids,
                decision.approved_remove_role_ids,
                ui,
            ),
            inline=False,
        )
    if decision.status in {
        RoleRequestStatus.PARTIALLY_APPROVED,
        RoleRequestStatus.REJECTED,
        RoleRequestStatus.FAILED,
    }:
        embed.add_field(
            name=ui.unapproved_changes_title,
            value=_render_changes(
                decision.unapproved_add_role_ids,
                decision.unapproved_remove_role_ids,
                ui,
            ),
            inline=False,
        )
    embed.add_field(
        name=ui.reviewer_field_title,
        value=f"<@{decision.reviewer_id}> · <t:{int(decision.decided_at.timestamp())}:f>",
        inline=False,
    )
    if decision.comment:
        embed.add_field(
            name=ui.decision_comment_title,
            value=decision.comment,
            inline=False,
        )
    embed.set_footer(text=f"Solicitud {request.message_id} · {CODEC_VERSION}")
    return embed


def decode_pending_message(
    message: discord.Message,
    *,
    bot_user_id: int,
    allowed_channel_ids: frozenset[int] | None,
) -> RoleRequest:
    """Recover a pending request after validating its complete Discord envelope."""

    guild = message.guild
    if guild is None:
        raise RoleRequestCodecError("role request must belong to a guild")
    if message.author.id != bot_user_id:
        raise RoleRequestCodecError("role request message was not authored by this bot")
    if not isinstance(message.channel, discord.TextChannel):
        raise RoleRequestCodecError("role request must remain in a text channel")
    if allowed_channel_ids is not None and message.channel.id not in allowed_channel_ids:
        raise RoleRequestCodecError("role request channel is not allowed")
    if len(message.embeds) != 1:
        raise RoleRequestCodecError("role request must contain exactly one embed")

    embed = message.embeds[0]
    footer = embed.footer
    footer_match = FOOTER_PATTERN.fullmatch(footer.text if footer and footer.text else "")
    if footer_match is None or int(footer_match.group("message_id")) != message.id:
        raise RoleRequestCodecError("role request footer does not match its message")
    author = embed.author
    author_match = USER_URL_PATTERN.fullmatch(author.url if author and author.url else "")
    if author_match is None:
        raise RoleRequestCodecError("role request author URL is invalid")
    if len(embed.fields) != 4:
        raise RoleRequestCodecError("role request embed field layout is invalid")
    _validate_pending_components(message)

    add_role_ids = _parse_role_mentions(embed.fields[0].value)
    remove_role_ids = _parse_role_mentions(embed.fields[1].value)
    reason_value = embed.fields[2].value.strip()
    created_at = embed.timestamp or message.created_at
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=UTC)
    return RoleRequest(
        message_id=message.id,
        guild_id=guild.id,
        channel_id=message.channel.id,
        requester_id=int(author_match.group("user_id")),
        add_role_ids=add_role_ids,
        remove_role_ids=remove_role_ids,
        reason=None if reason_value == EMPTY_VALUE else reason_value,
        created_at=created_at,
    )


def _validate_pending_components(message: discord.Message) -> None:
    components: dict[str, bool] = {}
    for row in message.components:
        for child in getattr(row, "children", ()):  # Pycord action rows expose children.
            custom_id = getattr(child, "custom_id", None)
            if custom_id in DECISION_CUSTOM_IDS:
                components[custom_id] = bool(getattr(child, "disabled", False))
    if set(components) != DECISION_CUSTOM_IDS or any(components.values()):
        raise RoleRequestCodecError("role request is no longer pending")


def _parse_role_mentions(value: str) -> tuple[int, ...]:
    normalized = value.strip()
    if normalized in {EMPTY_VALUE, ""}:
        return ()
    role_ids: list[int] = []
    for line in normalized.splitlines():
        match = ROLE_MENTION_PATTERN.fullmatch(line.strip())
        if match is None:
            raise RoleRequestCodecError("role request contains malformed role mentions")
        role_ids.append(int(match.group("role_id")))
    return tuple(role_ids)


def _render_role_mentions(role_ids: Iterable[int], empty: str) -> str:
    values = tuple(role_ids)
    return "\n".join(f"<@&{role_id}>" for role_id in values) if values else empty


def _render_changes(
    add_role_ids: Iterable[int],
    remove_role_ids: Iterable[int],
    ui: PersonaRoleRequestMessages,
) -> str:
    return (
        f"**{ui.add_change_prefix}:** {_render_role_mentions(add_role_ids, EMPTY_VALUE)}\n"
        f"**{ui.remove_change_prefix}:** "
        f"{_render_role_mentions(remove_role_ids, EMPTY_VALUE)}"
    )


def _decision_presentation(
    status: RoleRequestStatus,
    ui: PersonaRoleRequestMessages,
) -> tuple[str, str, discord.Color]:
    if status is RoleRequestStatus.APPROVED:
        return ui.status_approved, ui.status_approved, discord.Color.green()
    if status is RoleRequestStatus.PARTIALLY_APPROVED:
        return ui.status_partial, ui.status_partial, discord.Color.orange()
    if status is RoleRequestStatus.REJECTED:
        return ui.status_rejected, ui.status_rejected, discord.Color.red()
    return ui.status_failed, ui.status_failed, discord.Color.dark_grey()

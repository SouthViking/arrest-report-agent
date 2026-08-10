"""Discord commands, modals and persistent controls for role requests."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime

import discord
from discord.ext import commands

from arrest_report_agent.config import AppSettings, BotPersona
from arrest_report_agent.config.logging import exception_fields, log_event

from .codec import (
    APPROVE_ALL_CUSTOM_ID,
    APPROVE_PARTIAL_CUSTOM_ID,
    REJECT_CUSTOM_ID,
    RoleRequestCodecError,
    build_decision_embed,
    build_pending_embed,
    decode_pending_message,
)
from .models import (
    MAX_ROLES_PER_OPERATION,
    RoleRequest,
    RoleRequestDecision,
    RoleRequestStatus,
)

LOGGER = logging.getLogger(__name__)
MODAL_TIMEOUT_SECONDS = 300
MAX_REASON_LENGTH = 1000
MAX_STRING_SELECT_OPTIONS = 25
MAX_REQUESTABLE_ROLES_PER_GUILD = MAX_STRING_SELECT_OPTIONS * 2


@dataclass(frozen=True, slots=True)
class RoleRequestModalCopy:
    """Neutral copy shared by every persona for role-request forms."""

    request_title: str = "Solicitar cambios de roles"
    add_roles_label: str = "Roles para agregar"
    add_roles_placeholder: str = "Selecciona hasta diez roles que todavía no tengas."
    remove_roles_label: str = "Roles para retirar"
    remove_roles_placeholder: str = "Selecciona hasta diez roles que tengas actualmente."
    request_reason_label: str = "Motivo (opcional)"
    request_reason_placeholder: str = "Explica brevemente el cambio solicitado."
    partial_title: str = "Aprobar parcialmente"
    partial_add_label: str = "Roles que se agregarán"
    partial_remove_label: str = "Roles que se retirarán"
    partial_comment_label: str = "Comentario (opcional)"
    partial_comment_placeholder: str = "Explica la decisión si es necesario."
    reject_title: str = "Rechazar solicitud"
    reject_reason_label: str = "Motivo del rechazo"
    reject_reason_placeholder: str = "Explica por qué no se autorizan los cambios."


ROLE_REQUEST_MODAL_COPY = RoleRequestModalCopy()


class RoleRequestValidationError(ValueError):
    """Expected, user-facing rejection before a request is published."""

    def __init__(self, public_message: str) -> None:
        super().__init__(public_message)
        self.public_message = public_message


class RoleRequestService:
    """Orchestrate role requests while Discord messages remain the only storage."""

    def __init__(self, bot: discord.Bot, settings: AppSettings, persona: BotPersona) -> None:
        self.bot = bot
        self.settings = settings
        self.persona = persona
        self.ui = persona.messages.interface.role_requests
        self.allowed_channels = settings.role_request_allowed_channel_allowlist
        self.reviewer_role_ids = frozenset(settings.role_request_reviewer_role_ids)
        self.allowed_role_ids = frozenset(settings.role_request_allowed_role_ids)
        self.allowed_role_order = settings.role_request_allowed_role_ids
        self._locks: dict[int, asyncio.Lock] = {}
        self._lock_users: dict[int, int] = {}
        self._creation_locks: dict[tuple[int, int], asyncio.Lock] = {}
        self._creation_lock_users: dict[tuple[int, int], int] = {}

    def build_alert_embed(self, message: str) -> discord.Embed:
        """Render a consistent, personality-aware alert for expected failures."""

        return self.build_notice_embed(
            title=self.ui.alert_title,
            description=message,
            color=discord.Color.red(),
        )

    @staticmethod
    def build_notice_embed(
        *,
        title: str,
        description: str | None = None,
        color: discord.Color,
    ) -> discord.Embed:
        """Render user-facing role-request feedback without plain-text replies."""

        return discord.Embed(title=title, description=description, color=color)

    def is_reviewer(self, member: discord.Member) -> bool:
        return any(role.id in self.reviewer_role_ids for role in member.roles)

    def channel_is_allowed(self, channel: discord.abc.GuildChannel) -> bool:
        return isinstance(channel, discord.TextChannel) and self.allowed_channels.allows(channel.id)

    async def resolve_allowed_channels(
        self,
        guild: discord.Guild,
    ) -> tuple[discord.TextChannel, ...]:
        """Resolve text channels from this guild where requests may be published."""

        bot_member = guild.me
        if bot_member is None:
            return ()
        candidates: Iterable[discord.abc.GuildChannel]
        if self.allowed_channels.ids is None:
            candidates = guild.text_channels
        else:
            candidates = tuple(
                channel
                for channel_id in sorted(self.allowed_channels.ids)
                if (channel := guild.get_channel(channel_id)) is not None
            )
        result: list[discord.TextChannel] = []
        for channel in candidates:
            if not isinstance(channel, discord.TextChannel):
                continue
            permissions = channel.permissions_for(bot_member)
            if permissions.view_channel and permissions.read_message_history:
                result.append(channel)
        return tuple(result)

    async def allowed_channel_summary(self, guild: discord.Guild) -> str:
        if self.allowed_channels.ids is None:
            return self.ui.info_all_channels
        channels = await self.resolve_allowed_channels(guild)
        return (
            "\n".join(f"• {channel.mention}" for channel in channels)
            or self.ui.no_available_channels
        )

    def validate_channel_permissions(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
    ) -> None:
        bot_member = guild.me
        if bot_member is None:
            raise RoleRequestValidationError(self.ui.subsystem_unavailable)
        permissions = channel.permissions_for(bot_member)
        if not (
            permissions.view_channel
            and permissions.send_messages
            and permissions.embed_links
            and permissions.read_message_history
            and bot_member.guild_permissions.manage_roles
        ):
            raise RoleRequestValidationError(self.ui.subsystem_unavailable)

    def validate_submission(
        self,
        member: discord.Member,
        add_roles: Iterable[discord.Role],
        remove_roles: Iterable[discord.Role],
    ) -> tuple[tuple[discord.Role, ...], tuple[discord.Role, ...]]:
        additions = _unique_roles(add_roles)
        removals = _unique_roles(remove_roles)
        if not additions and not removals:
            raise RoleRequestValidationError(self.ui.no_changes)
        if (
            len(additions) > MAX_ROLES_PER_OPERATION
            or len(removals) > MAX_ROLES_PER_OPERATION
        ):
            raise RoleRequestValidationError(
                self.ui.too_many_roles.format(limit=MAX_ROLES_PER_OPERATION)
            )

        add_ids = {role.id for role in additions}
        remove_ids = {role.id for role in removals}
        unlisted = tuple(
            role
            for role in (*additions, *removals)
            if role.id not in self.allowed_role_ids
            or getattr(role.guild, "id", None) != member.guild.id
        )
        if unlisted:
            raise RoleRequestValidationError(
                self.ui.unlisted_roles.format(
                    roles=_role_mentions(role.id for role in unlisted)
                )
            )
        overlap = add_ids & remove_ids
        if overlap:
            raise RoleRequestValidationError(
                self.ui.overlapping_roles.format(roles=_role_mentions(overlap))
            )

        current_ids = {role.id for role in member.roles}
        already_owned = add_ids & current_ids
        if already_owned:
            raise RoleRequestValidationError(
                self.ui.already_owned_roles.format(roles=_role_mentions(already_owned))
            )
        missing = remove_ids - current_ids
        if missing:
            raise RoleRequestValidationError(
                self.ui.missing_roles.format(roles=_role_mentions(missing))
            )

        bot_member = member.guild.me
        if bot_member is None:
            raise RoleRequestValidationError(self.ui.subsystem_unavailable)
        protected = tuple(
            role for role in (*additions, *removals) if self._role_is_protected(role, bot_member)
        )
        if protected:
            raise RoleRequestValidationError(
                self.ui.protected_roles.format(
                    roles=_role_mentions(role.id for role in protected)
                )
            )
        return additions, removals

    def requestable_role_choices(
        self,
        member: discord.Member,
    ) -> tuple[tuple[discord.Role, ...], tuple[discord.Role, ...]]:
        """Resolve safe add/remove choices from the configured catalog for one guild."""

        bot_member = member.guild.me
        if bot_member is None:
            raise RoleRequestValidationError(self.ui.subsystem_unavailable)
        catalog: list[discord.Role] = []
        unavailable_ids: list[int] = []
        for role_id in self.allowed_role_order:
            role = member.guild.get_role(role_id)
            if role is None:
                continue
            if self._role_is_protected(role, bot_member):
                unavailable_ids.append(role_id)
                continue
            catalog.append(role)
        if unavailable_ids:
            log_event(
                LOGGER,
                "warning",
                "role_request_configured_roles_unmanageable",
                guild_id=member.guild.id,
                role_ids=unavailable_ids,
                role_count=len(unavailable_ids),
            )
        if not catalog:
            raise RoleRequestValidationError(self.ui.no_requestable_roles)
        if len(catalog) > MAX_REQUESTABLE_ROLES_PER_GUILD:
            log_event(
                LOGGER,
                "error",
                "role_request_catalog_too_large",
                guild_id=member.guild.id,
                role_count=len(catalog),
                maximum=MAX_REQUESTABLE_ROLES_PER_GUILD,
            )
            raise RoleRequestValidationError(self.ui.subsystem_unavailable)

        current_ids = {role.id for role in member.roles}
        additions = tuple(role for role in catalog if role.id not in current_ids)
        removals = tuple(role for role in catalog if role.id in current_ids)
        return additions, removals

    def resolve_selected_roles(
        self,
        guild: discord.Guild,
        raw_role_ids: Iterable[str],
    ) -> tuple[discord.Role, ...]:
        """Resolve untrusted select values back to configured roles."""

        roles: list[discord.Role] = []
        for raw_role_id in raw_role_ids:
            try:
                role_id = int(raw_role_id)
            except (TypeError, ValueError) as error:
                raise RoleRequestValidationError(self.ui.request_unavailable) from error
            if role_id not in self.allowed_role_ids:
                raise RoleRequestValidationError(self.ui.request_unavailable)
            role = guild.get_role(role_id)
            if role is None:
                raise RoleRequestValidationError(self.ui.request_unavailable)
            roles.append(role)
        return _unique_roles(roles)

    async def find_pending_request(
        self,
        guild: discord.Guild,
        requester_id: int,
    ) -> discord.Message | None:
        bot_user = self.bot.user
        if bot_user is None:
            return None
        for channel in await self.resolve_allowed_channels(guild):
            try:
                async for message in channel.history(limit=None, oldest_first=False):
                    try:
                        request = decode_pending_message(
                            message,
                            bot_user_id=bot_user.id,
                            allowed_channel_ids=self.allowed_channels.ids,
                        )
                    except (RoleRequestCodecError, ValueError):
                        continue
                    if request.requester_id == requester_id:
                        return message
            except (discord.Forbidden, discord.HTTPException) as error:
                log_event(
                    LOGGER,
                    "warning",
                    "role_request_pending_scan_failed",
                    exc_info=error,
                    guild_id=guild.id,
                    channel_id=channel.id,
                    requester_id=requester_id,
                    **exception_fields(error),
                )
        return None

    async def create_request(
        self,
        interaction: discord.Interaction,
        *,
        member: discord.Member,
        channel: discord.TextChannel,
        add_roles: Iterable[discord.Role],
        remove_roles: Iterable[discord.Role],
        reason: str | None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            async with self._creation_lock(member.guild.id, member.id):
                message, additions, removals = await self._publish_new_request(
                    member=member,
                    channel=channel,
                    add_roles=add_roles,
                    remove_roles=remove_roles,
                    reason=reason,
                )
            log_event(
                LOGGER,
                "info",
                "role_request_created",
                guild_id=member.guild.id,
                channel_id=channel.id,
                message_id=message.id,
                requester_id=member.id,
                add_role_count=len(additions),
                remove_role_count=len(removals),
            )
            try:
                await interaction.followup.send(
                    embed=self.build_notice_embed(
                        title=self.ui.pending_title,
                        description=self.ui.created_confirmation.format(
                            jump_url=message.jump_url
                        ),
                        color=discord.Color.gold(),
                    ),
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except (discord.Forbidden, discord.HTTPException) as error:
                log_event(
                    LOGGER,
                    "warning",
                    "role_request_confirmation_failed",
                    exc_info=error,
                    guild_id=member.guild.id,
                    channel_id=channel.id,
                    message_id=message.id,
                    requester_id=member.id,
                    **exception_fields(error),
                )
        except RoleRequestValidationError as error:
            await interaction.followup.send(
                embed=self.build_alert_embed(error.public_message),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as error:
            log_event(
                LOGGER,
                "error",
                "role_request_creation_failed",
                exc_info=error,
                guild_id=member.guild.id,
                channel_id=channel.id,
                requester_id=member.id,
                **exception_fields(error),
            )
            await interaction.followup.send(
                embed=self.build_alert_embed(self.ui.operation_failed),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def _publish_new_request(
        self,
        *,
        member: discord.Member,
        channel: discord.TextChannel,
        add_roles: Iterable[discord.Role],
        remove_roles: Iterable[discord.Role],
        reason: str | None,
    ) -> tuple[discord.Message, tuple[discord.Role, ...], tuple[discord.Role, ...]]:
        if (
            channel.guild.id != member.guild.id
            or not self.settings.guild_allowlist.allows(member.guild.id)
            or not self.channel_is_allowed(channel)
        ):
            raise RoleRequestValidationError(self.ui.request_unavailable)
        self.validate_channel_permissions(member.guild, channel)
        additions, removals = self.validate_submission(member, add_roles, remove_roles)
        pending = await self.find_pending_request(member.guild, member.id)
        if pending is not None:
            raise RoleRequestValidationError(
                self.ui.pending_exists.format(jump_url=pending.jump_url)
            )

        normalized_reason = (reason or "").strip() or None
        created_at = datetime.now(UTC)
        draft = build_pending_embed(
            requester=member,
            add_role_ids=(role.id for role in additions),
            remove_role_ids=(role.id for role in removals),
            reason=normalized_reason,
            ui=self.ui,
            created_at=created_at,
        )
        message = await channel.send(
            embed=draft,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        embed = build_pending_embed(
            requester=member,
            add_role_ids=(role.id for role in additions),
            remove_role_ids=(role.id for role in removals),
            reason=normalized_reason,
            ui=self.ui,
            created_at=created_at,
            message_id=message.id,
        )
        try:
            await message.edit(
                embed=embed,
                view=RoleRequestDecisionView(self),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            with suppress(discord.Forbidden, discord.HTTPException):
                await message.delete()
            raise
        return message, additions, removals

    async def authorize_reviewer_interaction(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        member = interaction.user
        guild = interaction.guild
        if (
            guild is None
            or not self.settings.guild_allowlist.allows(guild.id)
            or not isinstance(member, discord.Member)
            or not self.is_reviewer(member)
        ):
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    embed=self.build_alert_embed(self.ui.reviewer_unauthorized),
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            log_event(
                LOGGER,
                "warning",
                "role_request_reviewer_unauthorized",
                guild_id=guild.id if guild else "direct_message",
                user_id=member.id,
                message_id=getattr(interaction.message, "id", "none"),
            )
            return False
        return True

    def decode_interaction_message(self, interaction: discord.Interaction) -> RoleRequest:
        message = interaction.message
        bot_user = self.bot.user
        if message is None or bot_user is None:
            raise RoleRequestCodecError("role request interaction has no source message")
        return decode_pending_message(
            message,
            bot_user_id=bot_user.id,
            allowed_channel_ids=self.allowed_channels.ids,
        )

    async def approve_all(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._approve(interaction, selected_add=None, selected_remove=None, comment=None)

    async def approve_partial(
        self,
        interaction: discord.Interaction,
        *,
        guild_id: int,
        channel_id: int,
        message_id: int,
        selected_add: tuple[int, ...],
        selected_remove: tuple[int, ...],
        comment: str | None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        await self._approve(
            interaction,
            selected_add=selected_add,
            selected_remove=selected_remove,
            comment=(comment or "").strip() or None,
            expected=(guild_id, channel_id, message_id),
        )

    async def reject(
        self,
        interaction: discord.Interaction,
        *,
        guild_id: int,
        channel_id: int,
        message_id: int,
        reason: str,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if not await self._reauthorize_after_modal(interaction, guild_id):
            return
        try:
            async with self._decision_lock(message_id):
                message, request = await self._fetch_pending_request(
                    guild_id,
                    channel_id,
                    message_id,
                )
                context = await self._resolve_action_context(
                    message,
                    request,
                    interaction,
                )
                if context is None:
                    return
                decision = RoleRequestDecision(
                    status=RoleRequestStatus.REJECTED,
                    reviewer_id=interaction.user.id,
                    unapproved_add_role_ids=request.add_role_ids,
                    unapproved_remove_role_ids=request.remove_role_ids,
                    comment=reason.strip(),
                    decided_at=datetime.now(UTC),
                )
                await self._publish_decision(message, request, decision)
                log_event(
                    LOGGER,
                    "info",
                    "role_request_rejected",
                    guild_id=guild_id,
                    channel_id=channel_id,
                    message_id=message_id,
                    requester_id=request.requester_id,
                    reviewer_id=interaction.user.id,
                    add_role_count=len(request.add_role_ids),
                    remove_role_count=len(request.remove_role_ids),
                )
                await interaction.followup.send(
                    embed=self.build_notice_embed(
                        title=self.ui.status_rejected,
                        color=discord.Color.red(),
                    ),
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        except (RoleRequestCodecError, discord.NotFound) as error:
            await self._publish_stale(interaction, error, message_id=message_id)
        except Exception as error:
            await self._publish_operation_failure(
                interaction,
                error,
                event="role_request_rejection_failed",
                message_id=message_id,
            )

    async def _approve(
        self,
        interaction: discord.Interaction,
        *,
        selected_add: tuple[int, ...] | None,
        selected_remove: tuple[int, ...] | None,
        comment: str | None,
        expected: tuple[int, int, int] | None = None,
    ) -> None:
        try:
            if expected is None:
                request = self.decode_interaction_message(interaction)
                expected = (request.guild_id, request.channel_id, request.message_id)
            guild_id, channel_id, message_id = expected
            if not await self._reauthorize_after_modal(interaction, guild_id):
                return
        except RoleRequestCodecError as error:
            await self._publish_stale(
                interaction,
                error,
                message_id=getattr(interaction.message, "id", "none"),
            )
            return

        try:
            async with self._decision_lock(message_id):
                message, request = await self._fetch_pending_request(
                    guild_id,
                    channel_id,
                    message_id,
                )
                requested_add = set(request.add_role_ids)
                requested_remove = set(request.remove_role_ids)
                approved_add = requested_add if selected_add is None else set(selected_add)
                approved_remove = (
                    requested_remove if selected_remove is None else set(selected_remove)
                )
                if not approved_add <= requested_add or not approved_remove <= requested_remove:
                    raise RoleRequestCodecError("partial approval is not a request subset")
                if not approved_add and not approved_remove:
                    await interaction.followup.send(
                        embed=self.build_alert_embed(self.ui.partial_empty),
                        ephemeral=True,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    return

                context = await self._resolve_action_context(
                    message,
                    request,
                    interaction,
                )
                if context is None:
                    return
                guild, member = context

                current_ids = {
                    role.id for role in member.roles if role.id != guild.default_role.id
                }
                target_ids = set(current_ids)
                target_ids.update(approved_add)
                target_ids.difference_update(approved_remove)
                if target_ids != current_ids:
                    target_roles = sorted(
                        (
                            role
                            for role_id in target_ids
                            if (role := guild.get_role(role_id)) is not None
                        ),
                        key=lambda role: role.position,
                    )
                    await member.edit(
                        roles=target_roles,
                        reason=(
                            f"Solicitud de roles {request.message_id} aprobada por "
                            f"{interaction.user.id}"
                        ),
                    )

                unapproved_add = requested_add - approved_add
                unapproved_remove = requested_remove - approved_remove
                status = (
                    RoleRequestStatus.APPROVED
                    if not unapproved_add and not unapproved_remove
                    else RoleRequestStatus.PARTIALLY_APPROVED
                )
                decision = RoleRequestDecision(
                    status=status,
                    reviewer_id=interaction.user.id,
                    approved_add_role_ids=tuple(sorted(approved_add)),
                    approved_remove_role_ids=tuple(sorted(approved_remove)),
                    unapproved_add_role_ids=tuple(sorted(unapproved_add)),
                    unapproved_remove_role_ids=tuple(sorted(unapproved_remove)),
                    comment=comment,
                    decided_at=datetime.now(UTC),
                )
                await self._publish_decision(message, request, decision)
                log_event(
                    LOGGER,
                    "info",
                    "role_request_decided",
                    guild_id=guild_id,
                    channel_id=channel_id,
                    message_id=message_id,
                    requester_id=request.requester_id,
                    reviewer_id=interaction.user.id,
                    decision=status.value,
                    approved_add_role_count=len(approved_add),
                    approved_remove_role_count=len(approved_remove),
                    unapproved_add_role_count=len(unapproved_add),
                    unapproved_remove_role_count=len(unapproved_remove),
                )
                await interaction.followup.send(
                    embed=self.build_notice_embed(
                        title=(
                            self.ui.status_approved
                            if status is RoleRequestStatus.APPROVED
                            else self.ui.status_partial
                        ),
                        color=(
                            discord.Color.green()
                            if status is RoleRequestStatus.APPROVED
                            else discord.Color.gold()
                        ),
                    ),
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        except (RoleRequestCodecError, discord.NotFound) as error:
            await self._publish_stale(interaction, error, message_id=message_id)
        except Exception as error:
            await self._publish_operation_failure(
                interaction,
                error,
                event="role_request_approval_failed",
                message_id=message_id,
            )

    @asynccontextmanager
    async def _decision_lock(self, message_id: int):
        """Keep one shared lock alive while any decision is active or waiting."""

        lock = self._locks.setdefault(message_id, asyncio.Lock())
        self._lock_users[message_id] = self._lock_users.get(message_id, 0) + 1
        try:
            async with lock:
                yield
        finally:
            remaining = self._lock_users[message_id] - 1
            if remaining == 0:
                self._lock_users.pop(message_id, None)
                if self._locks.get(message_id) is lock:
                    self._locks.pop(message_id, None)
            else:
                self._lock_users[message_id] = remaining

    @asynccontextmanager
    async def _creation_lock(self, guild_id: int, requester_id: int):
        """Serialize the pending scan and publication for one requester."""

        key = (guild_id, requester_id)
        lock = self._creation_locks.setdefault(key, asyncio.Lock())
        self._creation_lock_users[key] = self._creation_lock_users.get(key, 0) + 1
        try:
            async with lock:
                yield
        finally:
            remaining = self._creation_lock_users[key] - 1
            if remaining == 0:
                self._creation_lock_users.pop(key, None)
                if self._creation_locks.get(key) is lock:
                    self._creation_locks.pop(key, None)
            else:
                self._creation_lock_users[key] = remaining

    async def _resolve_action_context(
        self,
        message: discord.Message,
        request: RoleRequest,
        interaction: discord.Interaction,
    ) -> tuple[discord.Guild, discord.Member] | None:
        """Revalidate the mutable Discord state immediately before a decision."""

        guild = self.bot.get_guild(request.guild_id)
        if guild is None:
            raise RoleRequestCodecError("role request guild is unavailable")
        try:
            member = await guild.fetch_member(request.requester_id)
        except discord.NotFound:
            await self._close_failed(
                message,
                request,
                reviewer_id=interaction.user.id,
                comment=self.ui.member_unavailable,
            )
            await interaction.followup.send(
                embed=self.build_alert_embed(self.ui.member_unavailable),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return None

        requested_roles = tuple(
            guild.get_role(role_id)
            for role_id in (*request.add_role_ids, *request.remove_role_ids)
        )
        bot_member = guild.me
        if (
            any(role is None for role in requested_roles)
            or bot_member is None
            or any(
                self._role_is_protected(role, bot_member)
                for role in requested_roles
                if role is not None
            )
        ):
            await self._close_failed(
                message,
                request,
                reviewer_id=interaction.user.id,
                comment=self.ui.request_unavailable,
            )
            await interaction.followup.send(
                embed=self.build_alert_embed(self.ui.request_unavailable),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return None
        return guild, member

    async def _fetch_pending_request(
        self,
        guild_id: int,
        channel_id: int,
        message_id: int,
    ) -> tuple[discord.Message, RoleRequest]:
        guild = self.bot.get_guild(guild_id)
        if guild is None or not self.settings.guild_allowlist.allows(guild_id):
            raise RoleRequestCodecError("role request guild is unavailable")
        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel) or not self.channel_is_allowed(channel):
            raise RoleRequestCodecError("role request channel is unavailable")
        message = await channel.fetch_message(message_id)
        bot_user = self.bot.user
        if bot_user is None:
            raise RoleRequestCodecError("bot identity is unavailable")
        request = decode_pending_message(
            message,
            bot_user_id=bot_user.id,
            allowed_channel_ids=self.allowed_channels.ids,
        )
        if (request.guild_id, request.channel_id, request.message_id) != (
            guild_id,
            channel_id,
            message_id,
        ):
            raise RoleRequestCodecError("role request envelope mismatch")
        return message, request

    async def _reauthorize_after_modal(
        self,
        interaction: discord.Interaction,
        expected_guild_id: int,
    ) -> bool:
        guild = interaction.guild
        member = interaction.user
        if (
            guild is None
            or guild.id != expected_guild_id
            or not isinstance(member, discord.Member)
            or not self.settings.guild_allowlist.allows(guild.id)
            or not self.is_reviewer(member)
        ):
            await interaction.followup.send(
                embed=self.build_alert_embed(self.ui.reviewer_unauthorized),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return False
        return True

    async def _publish_decision(
        self,
        message: discord.Message,
        request: RoleRequest,
        decision: RoleRequestDecision,
    ) -> None:
        guild = message.guild
        requester = guild.get_member(request.requester_id) if guild else None
        if requester is None and guild is not None:
            with suppress(discord.NotFound, discord.Forbidden, discord.HTTPException):
                requester = await guild.fetch_member(request.requester_id)
        requester_name = requester.display_name if requester else str(request.requester_id)
        avatar = getattr(getattr(requester, "display_avatar", None), "url", None)
        embed = build_decision_embed(
            request,
            decision,
            requester_name=requester_name,
            requester_avatar_url=str(avatar) if avatar else None,
            ui=self.ui,
        )
        await message.edit(
            embed=embed,
            view=RoleRequestDecisionView(self, disabled=True),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _close_failed(
        self,
        message: discord.Message,
        request: RoleRequest,
        *,
        reviewer_id: int,
        comment: str,
    ) -> None:
        decision = RoleRequestDecision(
            status=RoleRequestStatus.FAILED,
            reviewer_id=reviewer_id,
            unapproved_add_role_ids=request.add_role_ids,
            unapproved_remove_role_ids=request.remove_role_ids,
            comment=comment,
            decided_at=datetime.now(UTC),
        )
        await self._publish_decision(message, request, decision)
        log_event(
            LOGGER,
            "warning",
            "role_request_closed_failed",
            guild_id=request.guild_id,
            channel_id=request.channel_id,
            message_id=request.message_id,
            requester_id=request.requester_id,
            reviewer_id=reviewer_id,
            add_role_count=len(request.add_role_ids),
            remove_role_count=len(request.remove_role_ids),
        )

    async def _publish_stale(
        self,
        interaction: discord.Interaction,
        error: Exception,
        *,
        message_id: int | str,
    ) -> None:
        log_event(
            LOGGER,
            "warning",
            "role_request_stale",
            exc_info=error,
            guild_id=interaction.guild.id if interaction.guild else "direct_message",
            message_id=message_id,
            reviewer_id=interaction.user.id,
            **exception_fields(error),
        )
        await interaction.followup.send(
            embed=self.build_alert_embed(self.ui.stale_request),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _publish_operation_failure(
        self,
        interaction: discord.Interaction,
        error: Exception,
        *,
        event: str,
        message_id: int,
    ) -> None:
        log_event(
            LOGGER,
            "error",
            event,
            exc_info=error,
            guild_id=interaction.guild.id if interaction.guild else "direct_message",
            message_id=message_id,
            reviewer_id=interaction.user.id,
            **exception_fields(error),
        )
        await interaction.followup.send(
            embed=self.build_alert_embed(self.ui.operation_failed),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    def _role_is_protected(self, role: discord.Role, bot_member: discord.Member) -> bool:
        permissions = role.permissions
        return (
            role.id == role.guild.default_role.id
            or role.managed
            or role.id in self.reviewer_role_ids
            or role >= bot_member.top_role
            or permissions.administrator
            or permissions.manage_roles
            or permissions.manage_guild
        )


class RoleRequestDecisionView(discord.ui.View):
    """Persistent, generic controls resolved by the source message ID."""

    def __init__(self, service: RoleRequestService, *, disabled: bool = False) -> None:
        super().__init__(timeout=None)
        self.service = service
        ui = service.ui
        self.approve_all_button.label = ui.approve_all_button
        self.approve_partial_button.label = ui.approve_partial_button
        self.reject_button.label = ui.reject_button
        if disabled:
            for child in self.children:
                child.disabled = True

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await self.service.authorize_reviewer_interaction(interaction)

    @discord.ui.button(
        label="Aprobar todo",
        emoji="✅",
        style=discord.ButtonStyle.success,
        custom_id=APPROVE_ALL_CUSTOM_ID,
    )
    async def approve_all_button(
        self,
        _button: discord.ui.Button,
        interaction: discord.Interaction,
    ) -> None:
        await self.service.approve_all(interaction)

    @discord.ui.button(
        label="Aprobar parcialmente",
        emoji="🟡",
        style=discord.ButtonStyle.primary,
        custom_id=APPROVE_PARTIAL_CUSTOM_ID,
    )
    async def approve_partial_button(
        self,
        _button: discord.ui.Button,
        interaction: discord.Interaction,
    ) -> None:
        try:
            request = self.service.decode_interaction_message(interaction)
        except RoleRequestCodecError:
            await interaction.response.send_message(
                embed=self.service.build_alert_embed(self.service.ui.stale_request),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                embed=self.service.build_alert_embed(self.service.ui.request_unavailable),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await interaction.response.send_modal(
            PartialApprovalModal(self.service, request=request, guild=guild)
        )

    @discord.ui.button(
        label="Rechazar",
        emoji="❌",
        style=discord.ButtonStyle.danger,
        custom_id=REJECT_CUSTOM_ID,
    )
    async def reject_button(
        self,
        _button: discord.ui.Button,
        interaction: discord.Interaction,
    ) -> None:
        try:
            request = self.service.decode_interaction_message(interaction)
        except RoleRequestCodecError:
            await interaction.response.send_message(
                embed=self.service.build_alert_embed(self.service.ui.stale_request),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await interaction.response.send_modal(RejectRoleRequestModal(self.service, request))


class CreateRoleRequestModal(discord.ui.DesignerModal):
    def __init__(
        self,
        service: RoleRequestService,
        *,
        member: discord.Member,
        channel: discord.TextChannel,
    ) -> None:
        self.service = service
        self.member = member
        self.channel = channel
        copy = ROLE_REQUEST_MODAL_COPY
        additions, removals = service.requestable_role_choices(member)
        self.add_selects = _request_role_selects(
            additions,
            operation="add",
            placeholder=copy.add_roles_placeholder,
        )
        self.remove_selects = _request_role_selects(
            removals,
            operation="remove",
            placeholder=copy.remove_roles_placeholder,
        )
        self.reason_input = discord.ui.InputText(
            custom_id="role-request:v1:create:reason",
            style=discord.InputTextStyle.long,
            placeholder=copy.request_reason_placeholder,
            required=False,
            max_length=MAX_REASON_LENGTH,
        )
        children: list[discord.ui.Item] = []
        children.extend(
            _labeled_role_selects(copy.add_roles_label, self.add_selects)
        )
        children.extend(
            _labeled_role_selects(copy.remove_roles_label, self.remove_selects)
        )
        children.append(discord.ui.Label(copy.request_reason_label, self.reason_input))
        super().__init__(
            *children,
            title=copy.request_title,
            custom_id=f"role-request:v1:create:{member.id}",
            timeout=MODAL_TIMEOUT_SECONDS,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member) or member.id != self.member.id:
            await interaction.response.send_message(
                embed=self.service.build_alert_embed(self.service.ui.member_unavailable),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        try:
            add_roles = self.service.resolve_selected_roles(
                member.guild,
                _selected_values(self.add_selects),
            )
            remove_roles = self.service.resolve_selected_roles(
                member.guild,
                _selected_values(self.remove_selects),
            )
        except RoleRequestValidationError as error:
            await interaction.response.send_message(
                embed=self.service.build_alert_embed(error.public_message),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await self.service.create_request(
            interaction,
            member=member,
            channel=self.channel,
            add_roles=add_roles,
            remove_roles=remove_roles,
            reason=self.reason_input.value,
        )


class PartialApprovalModal(discord.ui.DesignerModal):
    def __init__(
        self,
        service: RoleRequestService,
        *,
        request: RoleRequest,
        guild: discord.Guild,
    ) -> None:
        self.service = service
        self.request = request
        ui = service.ui
        copy = ROLE_REQUEST_MODAL_COPY
        children = []
        self.add_select: discord.ui.StringSelect | None = None
        self.remove_select: discord.ui.StringSelect | None = None
        if request.add_role_ids:
            self.add_select = _requested_role_select(
                guild,
                request.add_role_ids,
                custom_id="role-request:v1:partial:add",
                unknown_role_label=ui.unknown_role_label,
            )
            children.append(discord.ui.Label(copy.partial_add_label, self.add_select))
        if request.remove_role_ids:
            self.remove_select = _requested_role_select(
                guild,
                request.remove_role_ids,
                custom_id="role-request:v1:partial:remove",
                unknown_role_label=ui.unknown_role_label,
            )
            children.append(discord.ui.Label(copy.partial_remove_label, self.remove_select))
        self.comment_input = discord.ui.InputText(
            custom_id="role-request:v1:partial:comment",
            style=discord.InputTextStyle.long,
            placeholder=copy.partial_comment_placeholder,
            required=False,
            max_length=MAX_REASON_LENGTH,
        )
        children.append(discord.ui.Label(copy.partial_comment_label, self.comment_input))
        super().__init__(
            *children,
            title=copy.partial_title,
            custom_id=f"role-request:v1:partial:{request.message_id}",
            timeout=MODAL_TIMEOUT_SECONDS,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.service.approve_partial(
            interaction,
            guild_id=self.request.guild_id,
            channel_id=self.request.channel_id,
            message_id=self.request.message_id,
            selected_add=tuple(int(value) for value in self.add_select.values)
            if self.add_select
            else (),
            selected_remove=tuple(int(value) for value in self.remove_select.values)
            if self.remove_select
            else (),
            comment=self.comment_input.value,
        )


class RejectRoleRequestModal(discord.ui.DesignerModal):
    def __init__(self, service: RoleRequestService, request: RoleRequest) -> None:
        self.service = service
        self.request = request
        copy = ROLE_REQUEST_MODAL_COPY
        self.reason_input = discord.ui.InputText(
            custom_id="role-request:v1:reject:reason",
            style=discord.InputTextStyle.long,
            placeholder=copy.reject_reason_placeholder,
            required=True,
            min_length=1,
            max_length=MAX_REASON_LENGTH,
        )
        super().__init__(
            discord.ui.Label(copy.reject_reason_label, self.reason_input),
            title=copy.reject_title,
            custom_id=f"role-request:v1:reject:{request.message_id}",
            timeout=MODAL_TIMEOUT_SECONDS,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.service.reject(
            interaction,
            guild_id=self.request.guild_id,
            channel_id=self.request.channel_id,
            message_id=self.request.message_id,
            reason=self.reason_input.value,
        )


class RoleRequestCog(commands.Cog):
    """Root commands for the isolated role-request subsystem."""

    def __init__(self, service: RoleRequestService) -> None:
        self.service = service
        command_copy = service.persona.messages.interface.commands
        self.request_roles.description = command_copy.request_roles_description
        self.info_roles.description = command_copy.info_roles_description

    @discord.slash_command(
        name="solicitar-roles",
        description="Solicita cambios de roles para aprobación",
    )
    async def request_roles(self, ctx: discord.ApplicationContext) -> None:
        guild = ctx.guild
        member = ctx.author
        channel = ctx.channel
        ui = self.service.ui
        if guild is None or not self.service.settings.guild_allowlist.allows(guild.id):
            await ctx.respond(
                embed=self.service.build_alert_embed(ui.wrong_guild),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if not isinstance(member, discord.Member):
            await ctx.respond(
                embed=self.service.build_alert_embed(ui.member_unavailable),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if isinstance(channel, discord.Thread):
            await ctx.respond(
                embed=self.service.build_alert_embed(ui.thread_blocked),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if not isinstance(channel, discord.TextChannel):
            await ctx.respond(
                embed=self.service.build_alert_embed(
                    ui.wrong_channel.format(channels=ui.no_available_channels)
                ),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if not self.service.channel_is_allowed(channel):
            channels = await self.service.allowed_channel_summary(guild)
            await ctx.respond(
                embed=self.service.build_alert_embed(
                    ui.wrong_channel.format(channels=channels)
                ),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        try:
            self.service.validate_channel_permissions(guild, channel)
        except RoleRequestValidationError as error:
            await ctx.respond(
                embed=self.service.build_alert_embed(error.public_message),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        try:
            modal = CreateRoleRequestModal(
                self.service,
                member=member,
                channel=channel,
            )
        except RoleRequestValidationError as error:
            await ctx.respond(
                embed=self.service.build_alert_embed(error.public_message),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await ctx.send_modal(modal)

    @discord.slash_command(
        name="info-roles",
        description="Explica cómo solicitar y aprobar cambios de roles",
    )
    async def info_roles(self, ctx: discord.ApplicationContext) -> None:
        guild = ctx.guild
        ui = self.service.ui
        if guild is None or not self.service.settings.guild_allowlist.allows(guild.id):
            await ctx.respond(
                embed=self.service.build_alert_embed(ui.wrong_guild),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        channels = await self.service.allowed_channel_summary(guild)
        embed = discord.Embed(
            title=ui.info_title,
            description=ui.info_description,
            color=discord.Color.blurple(),
        )
        embed.add_field(name=ui.info_usage_title, value=ui.info_usage, inline=False)
        embed.add_field(name=ui.info_rules_title, value=ui.info_rules, inline=False)
        embed.add_field(name=ui.info_channels_title, value=channels, inline=False)
        embed.add_field(name=ui.info_outcomes_title, value=ui.info_outcomes, inline=False)
        avatar = getattr(getattr(self.service.bot.user, "display_avatar", None), "url", None)
        if avatar:
            embed.set_thumbnail(url=str(avatar))
        await ctx.respond(
            embed=embed,
            ephemeral=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )


def _requested_role_select(
    guild: discord.Guild,
    role_ids: tuple[int, ...],
    *,
    custom_id: str,
    unknown_role_label: str,
) -> discord.ui.StringSelect:
    options = []
    for role_id in role_ids:
        role = guild.get_role(role_id)
        options.append(
            discord.SelectOption(
                label=(
                    role.name
                    if role
                    else unknown_role_label.format(role_id=role_id)
                )[:100],
                value=str(role_id),
            )
        )
    return discord.ui.StringSelect(
        custom_id=custom_id,
        options=options,
        min_values=0,
        max_values=len(options),
        required=False,
    )


def _request_role_selects(
    roles: tuple[discord.Role, ...],
    *,
    operation: str,
    placeholder: str,
) -> tuple[discord.ui.StringSelect, ...]:
    selects: list[discord.ui.StringSelect] = []
    for index, start in enumerate(range(0, len(roles), MAX_STRING_SELECT_OPTIONS), start=1):
        group = roles[start : start + MAX_STRING_SELECT_OPTIONS]
        options = [
            discord.SelectOption(label=role.name[:100], value=str(role.id))
            for role in group
        ]
        selects.append(
            discord.ui.StringSelect(
                custom_id=f"role-request:v1:create:{operation}:{index}",
                options=options,
                placeholder=placeholder,
                min_values=0,
                max_values=min(MAX_ROLES_PER_OPERATION, len(options)),
                required=False,
            )
        )
    return tuple(selects)


def _labeled_role_selects(
    label: str,
    selects: tuple[discord.ui.StringSelect, ...],
) -> tuple[discord.ui.Label, ...]:
    total = len(selects)
    return tuple(
        discord.ui.Label(
            label if total == 1 else f"{label} · {index}/{total}",
            select,
        )
        for index, select in enumerate(selects, start=1)
    )


def _selected_values(selects: Iterable[discord.ui.StringSelect]) -> tuple[str, ...]:
    return tuple(value for select in selects for value in select.values)


def _unique_roles(roles: Iterable[discord.Role]) -> tuple[discord.Role, ...]:
    result: list[discord.Role] = []
    seen: set[int] = set()
    for role in roles:
        if role.id not in seen:
            result.append(role)
            seen.add(role.id)
    return tuple(result)


def _role_mentions(role_ids: Iterable[int]) -> str:
    return ", ".join(f"<@&{role_id}>" for role_id in sorted(set(role_ids)))

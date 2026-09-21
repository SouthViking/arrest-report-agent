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

from arrest_report_agent.config import (
    AccessPolicyKey,
    AccessPolicyMode,
    AppSettings,
    BotPersona,
    GuildRuntimeSettings,
    RoleRequestFttEntryOptions,
    RoleRequestFttWelcomeOptions,
)
from arrest_report_agent.config.logging import exception_fields, log_event

from .codec import (
    APPROVE_ALL_CUSTOM_ID,
    APPROVE_PARTIAL_CUSTOM_ID,
    FTT_ENTRY_PENDING_TITLE,
    REJECT_CUSTOM_ID,
    RoleRequestCodecError,
    build_decision_embed,
    build_pending_embed,
    build_private_pending_embed,
    build_public_decision_embed,
    build_public_pending_embed,
    build_requester_decision_embed,
    decode_pending_message,
)
from .models import (
    MAX_MANUAL_ROLES_PER_OPERATION,
    MAX_ROLES_PER_OPERATION,
    RoleRequest,
    RoleRequestDecision,
    RoleRequestKind,
    RoleRequestStatus,
    StoredRoleRequest,
    StoredRoleRequestStatus,
)
from .repository import PendingRoleRequestError, RoleRequestRepository

LOGGER = logging.getLogger(__name__)
MODAL_TIMEOUT_SECONDS = 300
MAX_REASON_LENGTH = 1000
MAX_STRING_SELECT_OPTIONS = 25
MAX_ROLE_SELECTS_PER_MODAL = 4
MAX_REQUESTABLE_ROLES_PER_GUILD = MAX_STRING_SELECT_OPTIONS * MAX_ROLE_SELECTS_PER_MODAL


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
    start_title: str = "¿Qué necesitas solicitar?"
    start_description: str = "Elige el tipo de solicitud que necesitas enviar."
    normal_button: str = "Cambiar roles"
    ftt_entry_button: str = "Ingreso FTT"
    ftt_entry_title: str = "Solicitar ingreso FTT"
    ftt_entry_station_label: str = "Estación de destino"
    ftt_entry_station_placeholder: str = "Selecciona Ganton o Dillimore."
    ftt_entry_already_complete: str = "Ya tienes todos los roles de este ingreso FTT."
    ftt_entry_partial_blocked: str = (
        "Las solicitudes de ingreso FTT solo pueden aprobarse o rechazarse completas."
    )
    retirement_button: str = "Solicitar retiro"
    retirement_title: str = "Solicitar retiro de la facción"
    retirement_reason_label: str = "Motivo (opcional)"
    retirement_reason_placeholder: str = "Puedes dejar una referencia breve para la revisión."
    retirement_confirm_label: str = "Confirmo que solicito mi retiro"
    retirement_confirmation_required: str = "Debes confirmar el retiro antes de enviar la solicitud."
    retirement_already_owned: str = "Ya tienes el rol de retiro asignado."
    retirement_partial_blocked: str = (
        "Las solicitudes de retiro solo pueden aprobarse o rechazarse completas."
    )
    partial_title: str = "Aprobar parcialmente"
    partial_add_label: str = "Roles que se agregarán"
    partial_remove_label: str = "Roles que se retirarán"
    partial_comment_label: str = "Motivo de la aprobación parcial"
    partial_comment_placeholder: str = "Explica por qué solo se autoriza una parte."
    reject_title: str = "Rechazar solicitud"
    reject_reason_label: str = "Motivo del rechazo"
    reject_reason_placeholder: str = "Explica por qué no se autorizan los cambios."


ROLE_REQUEST_MODAL_COPY = RoleRequestModalCopy()


def _partial_approval_blocked_message(kind: RoleRequestKind) -> str:
    if kind is RoleRequestKind.FTT_ENTRY:
        return ROLE_REQUEST_MODAL_COPY.ftt_entry_partial_blocked
    return ROLE_REQUEST_MODAL_COPY.retirement_partial_blocked


class RoleRequestValidationError(ValueError):
    """Expected, user-facing rejection before a request is published."""

    def __init__(self, public_message: str) -> None:
        super().__init__(public_message)
        self.public_message = public_message


class RoleRequestServiceRegistry:
    """Resolve an immutable role-request service for the interaction's guild."""

    def __init__(
        self,
        bot: discord.Bot,
        config_provider: object,
        repository: RoleRequestRepository | None = None,
    ) -> None:
        self.bot = bot
        self.config_provider = config_provider
        self.repository = repository
        self._services: dict[object, RoleRequestService] = {}

    async def get(self, guild_id: int) -> RoleRequestService:
        bundle = await self.config_provider.cached(guild_id)  # type: ignore[attr-defined]
        feature = bundle.snapshot.features.get("role_requests")
        reviewer_policy = bundle.snapshot.policy(AccessPolicyKey.ROLE_REQUEST_REVIEWER_ROLES)
        if feature is None or not feature.enabled or reviewer_policy.mode is not AccessPolicyMode.RESTRICTED:
            raise LookupError(f"Role requests are disabled for guild {guild_id}")
        key = bundle.snapshot.revision_id
        service = self._services.get(key)
        if service is None:
            service = RoleRequestService(
                self.bot,
                bundle.settings,
                bundle.persona.profile,
                repository=self.repository,
            )
            service.registry = self
            self._services[key] = service
        return service


class RoleRequestService:
    """Orchestrate role requests while Discord messages remain the only storage."""

    def __init__(
        self,
        bot: discord.Bot,
        settings: AppSettings | GuildRuntimeSettings,
        persona: BotPersona,
        repository: RoleRequestRepository | None = None,
    ) -> None:
        self.bot = bot
        self.settings = settings
        self.persona = persona
        self.ui = persona.messages.interface.role_requests
        self.allowed_channels = settings.role_request_allowed_channel_allowlist
        self.reviewer_role_ids = frozenset(settings.role_request_reviewer_role_ids)
        self.allowed_roles = settings.role_request_allowed_role_allowlist
        self.allowed_role_order = settings.role_request_allowed_role_ids
        self.retired_role_id = getattr(settings, "role_request_retired_role_id", None)
        self.ftt_entry_options: RoleRequestFttEntryOptions | None = getattr(
            settings,
            "role_request_ftt_entry",
            None,
        )
        ftp_request_options = getattr(settings, "ftp_request_options", None)
        self.ftp_request_channel_id: int | None = (
            ftp_request_options.channel_id if ftp_request_options is not None else None
        )
        self.review_channel_ids = tuple(getattr(settings, "role_request_review_channel_ids", ()))
        self.repository = repository
        self.registry: RoleRequestServiceRegistry | None = None
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

    @property
    def retirement_enabled(self) -> bool:
        return self.retired_role_id is not None

    @property
    def ftt_entry_enabled(self) -> bool:
        return self.ftt_entry_options is not None

    @property
    def start_view_enabled(self) -> bool:
        return self.retirement_enabled or self.ftt_entry_enabled

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
        return "\n".join(f"• {channel.mention}" for channel in channels) or self.ui.no_available_channels

    def resolve_review_channels(self, guild: discord.Guild) -> tuple[discord.TextChannel, ...]:
        """Resolve configured private mirrors only when the bot can publish there."""

        bot_member = guild.me
        if bot_member is None:
            return ()
        channels: list[discord.TextChannel] = []
        for channel_id in self.review_channel_ids:
            channel = guild.get_channel(channel_id)
            if not isinstance(channel, discord.TextChannel):
                continue
            permissions = channel.permissions_for(bot_member)
            if (
                permissions.view_channel
                and permissions.send_messages
                and permissions.embed_links
                and permissions.read_message_history
            ):
                channels.append(channel)
        return tuple(channels)

    def resolve_reviewer_roles(self, guild: discord.Guild) -> tuple[discord.Role, ...]:
        """Resolve this guild's configured review roles for a private notification."""
        return tuple(
            role for role_id in self.reviewer_role_ids if (role := guild.get_role(role_id)) is not None
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
        if len(additions) > MAX_MANUAL_ROLES_PER_OPERATION or len(removals) > MAX_MANUAL_ROLES_PER_OPERATION:
            raise RoleRequestValidationError(
                self.ui.too_many_roles.format(limit=MAX_MANUAL_ROLES_PER_OPERATION)
            )

        add_ids = {role.id for role in additions}
        remove_ids = {role.id for role in removals}
        if self.retired_role_id is not None and self.retired_role_id in (add_ids | remove_ids):
            raise RoleRequestValidationError(self.ui.request_unavailable)
        unlisted = tuple(
            role
            for role in (*additions, *removals)
            if not self.allowed_roles.allows(role.id) or getattr(role.guild, "id", None) != member.guild.id
        )
        if unlisted:
            raise RoleRequestValidationError(
                self.ui.unlisted_roles.format(roles=_role_mentions(role.id for role in unlisted))
            )
        overlap = add_ids & remove_ids
        if overlap:
            raise RoleRequestValidationError(self.ui.overlapping_roles.format(roles=_role_mentions(overlap)))

        current_ids = {role.id for role in member.roles}
        already_owned = add_ids & current_ids
        if already_owned:
            raise RoleRequestValidationError(
                self.ui.already_owned_roles.format(roles=_role_mentions(already_owned))
            )
        missing = remove_ids - current_ids
        if missing:
            raise RoleRequestValidationError(self.ui.missing_roles.format(roles=_role_mentions(missing)))

        bot_member = member.guild.me
        if bot_member is None:
            raise RoleRequestValidationError(self.ui.subsystem_unavailable)
        protected = tuple(
            role for role in (*additions, *removals) if self._role_is_protected(role, bot_member)
        )
        if protected:
            raise RoleRequestValidationError(
                self.ui.protected_roles.format(roles=_role_mentions(role.id for role in protected))
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
        if self.allowed_roles.is_restricted:
            candidates = tuple(
                role
                for role_id in self.allowed_role_order
                if (role := member.guild.get_role(role_id)) is not None
            )
        else:
            candidates = tuple(reversed(member.guild.roles))
        for role in candidates:
            if self._role_is_protected(role, bot_member):
                if self.allowed_roles.is_restricted:
                    unavailable_ids.append(role.id)
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
        current_ids = {role.id for role in member.roles}
        additions = tuple(
            role for role in catalog if role.id not in current_ids and role.id != self.retired_role_id
        )
        removals = tuple(
            role for role in catalog if role.id in current_ids and role.id != self.retired_role_id
        )
        if not additions and not removals:
            raise RoleRequestValidationError(self.ui.no_changes)
        select_count = _role_select_count(additions) + _role_select_count(removals)
        if len(catalog) > MAX_REQUESTABLE_ROLES_PER_GUILD or select_count > MAX_ROLE_SELECTS_PER_MODAL:
            log_event(
                LOGGER,
                "error",
                "role_request_catalog_too_large",
                guild_id=member.guild.id,
                role_count=len(catalog),
                select_count=select_count,
                maximum_roles=MAX_REQUESTABLE_ROLES_PER_GUILD,
                maximum_selects=MAX_ROLE_SELECTS_PER_MODAL,
            )
            raise RoleRequestValidationError(self.ui.subsystem_unavailable)
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
            if not self.allowed_roles.allows(role_id):
                raise RoleRequestValidationError(self.ui.request_unavailable)
            if self.retired_role_id is not None and role_id == self.retired_role_id:
                raise RoleRequestValidationError(self.ui.request_unavailable)
            role = guild.get_role(role_id)
            if role is None:
                raise RoleRequestValidationError(self.ui.request_unavailable)
            roles.append(role)
        return _unique_roles(roles)

    def retirement_changes(
        self,
        member: discord.Member,
    ) -> tuple[discord.Role, tuple[discord.Role, ...]]:
        """Resolve the configured retirement role and current managed roles atomically."""

        retired_role_id = self.retired_role_id
        bot_member = member.guild.me
        if (
            retired_role_id is None
            or bot_member is None
            or not self.allowed_roles.is_restricted
            or not self.allowed_roles.allows(retired_role_id)
        ):
            raise RoleRequestValidationError(self.ui.request_unavailable)
        retired_role = member.guild.get_role(retired_role_id)
        if retired_role is None or self._role_is_protected(retired_role, bot_member):
            raise RoleRequestValidationError(self.ui.request_unavailable)
        current_ids = {role.id for role in member.roles}
        if retired_role_id in current_ids:
            raise RoleRequestValidationError(ROLE_REQUEST_MODAL_COPY.retirement_already_owned)
        removals = self._retirement_removals(member, bot_member)
        return retired_role, removals

    def ftt_entry_changes(
        self,
        member: discord.Member,
        station_key: str,
    ) -> tuple[str, tuple[discord.Role, ...]]:
        """Resolve one configured FTT entry package and omit roles already owned."""

        options = self.ftt_entry_options
        if options is None or not self.allowed_roles.is_restricted:
            raise RoleRequestValidationError(self.ui.request_unavailable)
        station = next(
            (candidate for candidate in options.stations if candidate.key == station_key),
            None,
        )
        if station is None:
            raise RoleRequestValidationError(self.ui.request_unavailable)
        desired_ids = (*options.base_role_ids, station.role_id)
        current_ids = {role.id for role in member.roles}
        additions: list[discord.Role] = []
        for role_id in desired_ids:
            role = member.guild.get_role(role_id)
            if role is None or not self.allowed_roles.allows(role_id):
                raise RoleRequestValidationError(self.ui.request_unavailable)
            if role_id not in current_ids:
                additions.append(role)
        if not additions:
            raise RoleRequestValidationError(ROLE_REQUEST_MODAL_COPY.ftt_entry_already_complete)
        validated, _ = self.validate_submission(member, additions, ())
        return f"{station.label} · <@&{station.role_id}>", validated

    def _retirement_removals(
        self,
        member: discord.Member,
        bot_member: discord.Member,
    ) -> tuple[discord.Role, ...]:
        candidates = tuple(
            role
            for role in member.roles
            if role.id != self.retired_role_id
            and self.allowed_roles.allows(role.id)
            and role.id != role.guild.default_role.id
            and not role.managed
        )
        if any(role >= bot_member.top_role for role in candidates):
            raise RoleRequestValidationError(self.ui.subsystem_unavailable)
        if len(candidates) > MAX_ROLES_PER_OPERATION:
            raise RoleRequestValidationError(self.ui.subsystem_unavailable)
        return candidates

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
                    interaction_id=getattr(interaction, "id", None),
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
                        description=self.ui.created_confirmation.format(jump_url=message.jump_url),
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

    async def create_retirement_request(
        self,
        interaction: discord.Interaction,
        *,
        member: discord.Member,
        channel: discord.TextChannel,
        reason: str | None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            async with self._creation_lock(member.guild.id, member.id):
                message, retired_role, removals = await self._publish_new_retirement_request(
                    interaction_id=getattr(interaction, "id", None),
                    member=member,
                    channel=channel,
                    reason=reason,
                )
            log_event(
                LOGGER,
                "info",
                "role_retirement_request_created",
                guild_id=member.guild.id,
                channel_id=channel.id,
                message_id=message.id,
                requester_id=member.id,
                retired_role_id=retired_role.id,
                remove_role_count=len(removals),
            )
            with suppress(discord.Forbidden, discord.HTTPException):
                await interaction.followup.send(
                    embed=self.build_notice_embed(
                        title=self.ui.pending_title,
                        description=self.ui.created_confirmation.format(jump_url=message.jump_url),
                        color=discord.Color.gold(),
                    ),
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
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
                "role_retirement_request_creation_failed",
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

    async def create_ftt_entry_request(
        self,
        interaction: discord.Interaction,
        *,
        member: discord.Member,
        channel: discord.TextChannel,
        station_key: str,
    ) -> None:
        """Create one complete, deterministic faction-entry request."""

        await interaction.response.defer(ephemeral=True)
        try:
            async with self._creation_lock(member.guild.id, member.id):
                station, additions = self.ftt_entry_changes(member, station_key)
                message, additions, _ = await self._publish_new_request(
                    interaction_id=getattr(interaction, "id", None),
                    member=member,
                    channel=channel,
                    add_roles=additions,
                    remove_roles=(),
                    reason=station,
                    kind=RoleRequestKind.FTT_ENTRY,
                )
            log_event(
                LOGGER,
                "info",
                "role_ftt_entry_request_created",
                guild_id=member.guild.id,
                channel_id=channel.id,
                message_id=message.id,
                requester_id=member.id,
                station_key=station_key,
                add_role_count=len(additions),
            )
            with suppress(discord.Forbidden, discord.HTTPException):
                await interaction.followup.send(
                    embed=self.build_notice_embed(
                        title=FTT_ENTRY_PENDING_TITLE,
                        description=self.ui.created_confirmation.format(jump_url=message.jump_url),
                        color=discord.Color.gold(),
                    ),
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
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
                "role_ftt_entry_request_creation_failed",
                exc_info=error,
                guild_id=member.guild.id,
                channel_id=channel.id,
                requester_id=member.id,
                station_key=station_key,
                **exception_fields(error),
            )
            await interaction.followup.send(
                embed=self.build_alert_embed(self.ui.operation_failed),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def _publish_new_retirement_request(
        self,
        *,
        interaction_id: int | None = None,
        member: discord.Member,
        channel: discord.TextChannel,
        reason: str | None,
    ) -> tuple[discord.Message, discord.Role, tuple[discord.Role, ...]]:
        if (
            channel.guild.id != member.guild.id
            or not self.settings.guild_allowlist.allows(member.guild.id)
            or not self.channel_is_allowed(channel)
        ):
            raise RoleRequestValidationError(self.ui.request_unavailable)
        self.validate_channel_permissions(member.guild, channel)
        retired_role, removals = self.retirement_changes(member)
        if self.repository is not None:
            message = await self._publish_v2_request(
                interaction_id=interaction_id,
                member=member,
                channel=channel,
                add_roles=(retired_role,),
                remove_roles=removals,
                reason=reason,
                kind=RoleRequestKind.RETIREMENT,
            )
            return message, retired_role, removals
        pending = await self.find_pending_request(member.guild, member.id)
        if pending is not None:
            raise RoleRequestValidationError(self.ui.pending_exists.format(jump_url=pending.jump_url))

        normalized_reason = (reason or "").strip() or None
        created_at = datetime.now(UTC)
        draft = build_pending_embed(
            requester=member,
            add_role_ids=(retired_role.id,),
            remove_role_ids=(role.id for role in removals),
            reason=normalized_reason,
            ui=self.ui,
            created_at=created_at,
            kind=RoleRequestKind.RETIREMENT,
        )
        message = await channel.send(embed=draft, allowed_mentions=discord.AllowedMentions.none())
        embed = build_pending_embed(
            requester=member,
            add_role_ids=(retired_role.id,),
            remove_role_ids=(role.id for role in removals),
            reason=normalized_reason,
            ui=self.ui,
            created_at=created_at,
            message_id=message.id,
            kind=RoleRequestKind.RETIREMENT,
        )
        try:
            await message.edit(
                embed=embed,
                view=RoleRequestDecisionView(
                    self,
                    registry=self.registry,
                    include_partial=False,
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            with suppress(discord.Forbidden, discord.HTTPException):
                await message.delete()
            raise
        return message, retired_role, removals

    async def _publish_new_request(
        self,
        *,
        interaction_id: int | None = None,
        member: discord.Member,
        channel: discord.TextChannel,
        add_roles: Iterable[discord.Role],
        remove_roles: Iterable[discord.Role],
        reason: str | None,
        kind: RoleRequestKind = RoleRequestKind.NORMAL,
    ) -> tuple[discord.Message, tuple[discord.Role, ...], tuple[discord.Role, ...]]:
        if (
            channel.guild.id != member.guild.id
            or not self.settings.guild_allowlist.allows(member.guild.id)
            or not self.channel_is_allowed(channel)
        ):
            raise RoleRequestValidationError(self.ui.request_unavailable)
        self.validate_channel_permissions(member.guild, channel)
        additions, removals = self.validate_submission(member, add_roles, remove_roles)
        if self.repository is not None:
            message = await self._publish_v2_request(
                interaction_id=interaction_id,
                member=member,
                channel=channel,
                add_roles=additions,
                remove_roles=removals,
                reason=reason,
                kind=kind,
            )
            return message, additions, removals
        pending = await self.find_pending_request(member.guild, member.id)
        if pending is not None:
            raise RoleRequestValidationError(self.ui.pending_exists.format(jump_url=pending.jump_url))

        normalized_reason = (reason or "").strip() or None
        created_at = datetime.now(UTC)
        draft = build_pending_embed(
            requester=member,
            add_role_ids=(role.id for role in additions),
            remove_role_ids=(role.id for role in removals),
            reason=normalized_reason,
            ui=self.ui,
            created_at=created_at,
            kind=kind,
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
            kind=kind,
        )
        try:
            await message.edit(
                embed=embed,
                view=RoleRequestDecisionView(
                    self,
                    registry=self.registry,
                    include_partial=kind is RoleRequestKind.NORMAL,
                    kind=kind,
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception:
            with suppress(discord.Forbidden, discord.HTTPException):
                await message.delete()
            raise
        return message, additions, removals

    async def _publish_v2_request(
        self,
        *,
        interaction_id: int | None,
        member: discord.Member,
        channel: discord.TextChannel,
        add_roles: tuple[discord.Role, ...],
        remove_roles: tuple[discord.Role, ...],
        reason: str | None,
        kind: RoleRequestKind,
    ) -> discord.Message:
        """Publish a sanitized public card and complete private review mirrors."""

        repository = self.repository
        if repository is None or interaction_id is None:
            raise RoleRequestValidationError(self.ui.subsystem_unavailable)
        review_channels = self.resolve_review_channels(member.guild)
        if not review_channels:
            raise RoleRequestValidationError(self.ui.subsystem_unavailable)
        reviewer_roles = self.resolve_reviewer_roles(member.guild)
        if not reviewer_roles:
            raise RoleRequestValidationError(self.ui.subsystem_unavailable)
        pending = await self.find_pending_request(member.guild, member.id)
        if pending is not None:
            raise RoleRequestValidationError(self.ui.pending_exists.format(jump_url=pending.jump_url))

        normalized_reason = (reason or "").strip() or None
        add_ids = tuple(role.id for role in add_roles)
        remove_ids = tuple(role.id for role in remove_roles)
        try:
            stored, created = await repository.reserve(
                guild_id=member.guild.id,
                interaction_id=interaction_id,
                requester_id=member.id,
                kind=kind,
                add_role_ids=add_ids,
                remove_role_ids=remove_ids,
                reason=normalized_reason,
                public_channel_id=channel.id,
            )
        except PendingRoleRequestError as error:
            existing = error.request
            if existing.public_message_id is not None:
                jump_url = (
                    f"https://discord.com/channels/{existing.guild_id}/"
                    f"{existing.public_channel_id}/{existing.public_message_id}"
                )
                raise RoleRequestValidationError(self.ui.pending_exists.format(jump_url=jump_url)) from error
            raise RoleRequestValidationError(self.ui.request_unavailable) from error
        if not created:
            if stored.public_message_id is not None:
                jump_url = (
                    f"https://discord.com/channels/{stored.guild_id}/"
                    f"{stored.public_channel_id}/{stored.public_message_id}"
                )
                raise RoleRequestValidationError(self.ui.pending_exists.format(jump_url=jump_url))
            raise RoleRequestValidationError(self.ui.request_unavailable)

        public_message: discord.Message | None = None
        private_messages: list[discord.Message] = []
        try:
            public_message = await channel.send(
                embed=build_public_pending_embed(
                    requester=member,
                    add_role_ids=add_ids,
                    remove_role_ids=remove_ids,
                    ui=self.ui,
                    created_at=stored.created_at,
                    kind=kind,
                    reason=normalized_reason,
                ),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            private_embed = build_private_pending_embed(
                requester=member,
                add_role_ids=add_ids,
                remove_role_ids=remove_ids,
                reason=normalized_reason,
                ui=self.ui,
                created_at=stored.created_at,
                public_jump_url=public_message.jump_url,
                kind=kind,
            )
            reviewer_mentions = " ".join(role.mention for role in reviewer_roles)
            reviewer_allowed_mentions = discord.AllowedMentions(
                everyone=False,
                users=False,
                roles=list(reviewer_roles),
            )
            for review_channel in review_channels:
                try:
                    private_messages.append(
                        await review_channel.send(
                            content=reviewer_mentions,
                            embed=private_embed,
                            view=RoleRequestDecisionView(
                                self,
                                registry=self.registry,
                                include_partial=kind is RoleRequestKind.NORMAL,
                                kind=kind,
                            ),
                            allowed_mentions=reviewer_allowed_mentions,
                        )
                    )
                except (discord.Forbidden, discord.HTTPException) as error:
                    log_event(
                        LOGGER,
                        "warning",
                        "role_request_private_mirror_failed",
                        exc_info=error,
                        guild_id=member.guild.id,
                        channel_id=review_channel.id,
                        request_id=str(stored.id),
                        **exception_fields(error),
                    )
            if not private_messages:
                raise RoleRequestValidationError(self.ui.subsystem_unavailable)
            await repository.mark_published(
                stored.id,
                public_message_id=public_message.id,
                review_messages=tuple(
                    (private_message.channel.id, private_message.id) for private_message in private_messages
                ),
            )
            return public_message
        except Exception:
            await repository.mark_failed(stored.id)
            for published in (*private_messages, public_message):
                if published is not None:
                    with suppress(discord.Forbidden, discord.HTTPException):
                        await published.delete()
            raise

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

    async def resolve_interaction_request(
        self,
        interaction: discord.Interaction,
    ) -> tuple[RoleRequest, StoredRoleRequest | None]:
        """Resolve v2 private mirrors first, then compatible v1 public messages."""

        message = interaction.message
        guild = interaction.guild
        channel = getattr(interaction, "channel", None)
        if self.repository is not None and message is not None and guild is not None and channel is not None:
            stored = await self.repository.get_by_review_message(
                guild.id,
                channel.id,
                message.id,
            )
            if stored is not None:
                if stored.status is not StoredRoleRequestStatus.PENDING:
                    raise RoleRequestCodecError("role request is no longer pending")
                return (
                    stored.as_legacy_request(
                        source_channel_id=channel.id,
                        source_message_id=message.id,
                    ),
                    stored,
                )
        return self.decode_interaction_message(interaction), None

    async def approve_all(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=False)
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
        await interaction.response.defer(ephemeral=False)
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
        await interaction.response.defer(ephemeral=False)
        if not await self._reauthorize_after_modal(interaction, guild_id):
            return
        try:
            stored = await self._get_stored_review_request(guild_id, channel_id, message_id)
            if stored is not None:
                await self._reject_v2(interaction, stored=stored, reason=reason.strip())
                return
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
                request, stored = await self.resolve_interaction_request(interaction)
                expected = (request.guild_id, request.channel_id, request.message_id)
            else:
                stored = await self._get_stored_review_request(*expected)
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

        if stored is not None:
            try:
                await self._approve_v2(
                    interaction,
                    stored=stored,
                    selected_add=selected_add,
                    selected_remove=selected_remove,
                    comment=comment,
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
            return

        try:
            async with self._decision_lock(message_id):
                message, request = await self._fetch_pending_request(
                    guild_id,
                    channel_id,
                    message_id,
                )
                if request.kind is not RoleRequestKind.NORMAL and (
                    selected_add is not None or selected_remove is not None
                ):
                    await interaction.followup.send(
                        embed=self.build_alert_embed(_partial_approval_blocked_message(request.kind)),
                        ephemeral=True,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    return
                requested_add = set(request.add_role_ids)
                requested_remove = set(request.remove_role_ids)
                approved_add = requested_add if selected_add is None else set(selected_add)
                approved_remove = requested_remove if selected_remove is None else set(selected_remove)
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

                current_ids = {role.id for role in member.roles if role.id != guild.default_role.id}
                if request.kind is RoleRequestKind.RETIREMENT:
                    retired_role_id = self.retired_role_id
                    bot_member = guild.me
                    if retired_role_id is None or requested_add != {retired_role_id} or bot_member is None:
                        raise RoleRequestCodecError("retirement configuration changed")
                    approved_add = {retired_role_id}
                    approved_remove = {role.id for role in self._retirement_removals(member, bot_member)}
                    target_ids = (current_ids - approved_remove) | approved_add
                else:
                    target_ids = set(current_ids)
                    target_ids.update(approved_add)
                    target_ids.difference_update(approved_remove)
                if target_ids != current_ids:
                    target_roles = sorted(
                        (role for role_id in target_ids if (role := guild.get_role(role_id)) is not None),
                        key=lambda role: role.position,
                    )
                    await member.edit(
                        roles=target_roles,
                        reason=(
                            f"Solicitud de roles {request.message_id} aprobada por {interaction.user.id}"
                        ),
                    )

                unapproved_add = (
                    set() if request.kind is not RoleRequestKind.NORMAL else requested_add - approved_add
                )
                unapproved_remove = (
                    set()
                    if request.kind is not RoleRequestKind.NORMAL
                    else requested_remove - approved_remove
                )
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
                if status is RoleRequestStatus.APPROVED:
                    await self._publish_ftt_entry_welcome(request, member)
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
        except (RoleRequestCodecError, discord.NotFound) as error:
            await self._publish_stale(interaction, error, message_id=message_id)
        except Exception as error:
            await self._publish_operation_failure(
                interaction,
                error,
                event="role_request_approval_failed",
                message_id=message_id,
            )

    async def _get_stored_review_request(
        self,
        guild_id: int,
        channel_id: int,
        message_id: int,
    ) -> StoredRoleRequest | None:
        if self.repository is None:
            return None
        return await self.repository.get_by_review_message(guild_id, channel_id, message_id)

    async def _approve_v2(
        self,
        interaction: discord.Interaction,
        *,
        stored: StoredRoleRequest,
        selected_add: tuple[int, ...] | None,
        selected_remove: tuple[int, ...] | None,
        comment: str | None,
    ) -> None:
        repository = self.repository
        if repository is None:
            raise RoleRequestCodecError("role request repository is unavailable")
        if stored.kind is not RoleRequestKind.NORMAL and (
            selected_add is not None or selected_remove is not None
        ):
            await interaction.followup.send(
                embed=self.build_alert_embed(_partial_approval_blocked_message(stored.kind)),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if (selected_add is not None or selected_remove is not None) and not (comment or "").strip():
            await interaction.followup.send(
                embed=self.build_alert_embed("Debes explicar el motivo de la aprobación parcial."),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        claimed = await repository.claim_decision(stored.id)
        if claimed is None:
            await self._publish_stale(
                interaction,
                RoleRequestCodecError("role request was already claimed"),
                message_id=stored.public_message_id or "none",
            )
            return
        try:
            guild, member = await self._resolve_v2_member(claimed)
            requested_add = set(claimed.add_role_ids)
            requested_remove = set(claimed.remove_role_ids)
            approved_add = requested_add if selected_add is None else set(selected_add)
            approved_remove = requested_remove if selected_remove is None else set(selected_remove)
            if not approved_add <= requested_add or not approved_remove <= requested_remove:
                raise RoleRequestCodecError("partial approval is not a request subset")
            if not approved_add and not approved_remove:
                await repository.release_decision(claimed.id)
                await interaction.followup.send(
                    embed=self.build_alert_embed(self.ui.partial_empty),
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return

            current_ids = {role.id for role in member.roles if role.id != guild.default_role.id}
            if claimed.kind is RoleRequestKind.RETIREMENT:
                retired_role_id = self.retired_role_id
                bot_member = guild.me
                if retired_role_id is None or requested_add != {retired_role_id} or bot_member is None:
                    raise RoleRequestCodecError("retirement configuration changed")
                approved_add = {retired_role_id}
                approved_remove = {role.id for role in self._retirement_removals(member, bot_member)}
                target_ids = (current_ids - approved_remove) | approved_add
            else:
                target_ids = (current_ids | approved_add) - approved_remove
            if target_ids != current_ids:
                target_roles = sorted(
                    (role for role_id in target_ids if (role := guild.get_role(role_id)) is not None),
                    key=lambda role: role.position,
                )
                await member.edit(
                    roles=target_roles,
                    reason=(
                        f"Solicitud de roles {claimed.public_message_id or claimed.id} "
                        f"aprobada por {interaction.user.id}"
                    ),
                )
            unapproved_add = (
                set() if claimed.kind is not RoleRequestKind.NORMAL else requested_add - approved_add
            )
            unapproved_remove = (
                set() if claimed.kind is not RoleRequestKind.NORMAL else requested_remove - approved_remove
            )
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
                comment=(comment or "").strip() or None,
                decided_at=datetime.now(UTC),
            )
            decided = await repository.finalize(claimed.id, decision)
            await self._publish_v2_decision(decided, decision)
            if status is RoleRequestStatus.APPROVED:
                await self._publish_ftt_entry_welcome(decided, member)
            log_event(
                LOGGER,
                "info",
                "role_request_decided",
                guild_id=claimed.guild_id,
                channel_id=claimed.public_channel_id,
                message_id=claimed.public_message_id or "none",
                request_id=str(claimed.id),
                requester_id=claimed.requester_id,
                reviewer_id=interaction.user.id,
                decision=status.value,
            )
        except Exception:
            await repository.release_decision(claimed.id)
            raise

    async def _reject_v2(
        self,
        interaction: discord.Interaction,
        *,
        stored: StoredRoleRequest,
        reason: str,
    ) -> None:
        repository = self.repository
        if repository is None:
            raise RoleRequestCodecError("role request repository is unavailable")
        if not reason:
            await interaction.followup.send(
                embed=self.build_alert_embed("Debes indicar el motivo del rechazo."),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        claimed = await repository.claim_decision(stored.id)
        if claimed is None:
            await self._publish_stale(
                interaction,
                RoleRequestCodecError("role request was already claimed"),
                message_id=stored.public_message_id or "none",
            )
            return
        try:
            decision = RoleRequestDecision(
                status=RoleRequestStatus.REJECTED,
                reviewer_id=interaction.user.id,
                unapproved_add_role_ids=claimed.add_role_ids,
                unapproved_remove_role_ids=claimed.remove_role_ids,
                comment=reason,
                decided_at=datetime.now(UTC),
            )
            decided = await repository.finalize(claimed.id, decision)
            await self._publish_v2_decision(decided, decision)
            log_event(
                LOGGER,
                "info",
                "role_request_rejected",
                guild_id=claimed.guild_id,
                channel_id=claimed.public_channel_id,
                message_id=claimed.public_message_id or "none",
                request_id=str(claimed.id),
                requester_id=claimed.requester_id,
                reviewer_id=interaction.user.id,
            )
        except Exception:
            await repository.release_decision(claimed.id)
            raise

    async def _resolve_v2_member(
        self,
        stored: StoredRoleRequest,
    ) -> tuple[discord.Guild, discord.Member]:
        guild = self.bot.get_guild(stored.guild_id)
        if guild is None:
            raise RoleRequestCodecError("role request guild is unavailable")
        try:
            member = await guild.fetch_member(stored.requester_id)
        except discord.NotFound as error:
            raise RoleRequestCodecError("role request member is unavailable") from error
        bot_member = guild.me
        if stored.kind is RoleRequestKind.RETIREMENT:
            retired_role = guild.get_role(self.retired_role_id) if self.retired_role_id is not None else None
            invalid = (
                bot_member is None
                or retired_role is None
                or stored.add_role_ids != (self.retired_role_id,)
                or self._role_is_protected(retired_role, bot_member)
            )
        else:
            roles = tuple(
                guild.get_role(role_id) for role_id in (*stored.add_role_ids, *stored.remove_role_ids)
            )
            invalid = (
                bot_member is None
                or any(role is None for role in roles)
                or any(self._role_is_protected(role, bot_member) for role in roles if role is not None)
            )
        if invalid:
            raise RoleRequestCodecError("role request roles are unavailable")
        return guild, member

    async def _publish_ftt_entry_welcome(
        self,
        request: RoleRequest | StoredRoleRequest,
        member: discord.Member,
    ) -> None:
        """Publish the configured FTP welcome after a complete entry approval."""

        options = self.ftt_entry_options
        welcome = options.welcome if options is not None else None
        if request.kind is not RoleRequestKind.FTT_ENTRY or welcome is None:
            return
        guild = member.guild
        channel: object | None = guild.get_channel(welcome.channel_id)
        if channel is None:
            try:
                channel = await guild.fetch_channel(welcome.channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as error:
                self._log_ftt_welcome_failure(
                    "role_ftt_entry_welcome_channel_unavailable",
                    request,
                    error,
                )
                return
        if not isinstance(channel, discord.TextChannel):
            log_event(
                LOGGER,
                "warning",
                "role_ftt_entry_welcome_channel_invalid",
                guild_id=request.guild_id,
                channel_id=welcome.channel_id,
                requester_id=request.requester_id,
            )
            return
        try:
            await channel.send(
                content=member.mention,
                embed=build_ftt_entry_welcome_embed(
                    member,
                    welcome,
                    ftp_request_channel_id=self.ftp_request_channel_id,
                ),
                allowed_mentions=discord.AllowedMentions(
                    everyone=False,
                    users=[member],
                    roles=False,
                    replied_user=False,
                ),
            )
            log_event(
                LOGGER,
                "info",
                "role_ftt_entry_welcome_published",
                guild_id=request.guild_id,
                channel_id=channel.id,
                requester_id=request.requester_id,
            )
        except (discord.Forbidden, discord.HTTPException) as error:
            self._log_ftt_welcome_failure(
                "role_ftt_entry_welcome_publish_failed",
                request,
                error,
            )

    @staticmethod
    def _log_ftt_welcome_failure(
        event: str,
        request: RoleRequest | StoredRoleRequest,
        error: Exception,
    ) -> None:
        log_event(
            LOGGER,
            "warning",
            event,
            exc_info=error,
            guild_id=request.guild_id,
            requester_id=request.requester_id,
            **exception_fields(error),
        )

    async def _publish_v2_decision(
        self,
        stored: StoredRoleRequest,
        decision: RoleRequestDecision,
    ) -> None:
        repository = self.repository
        guild = self.bot.get_guild(stored.guild_id)
        if repository is None or guild is None or stored.public_message_id is None:
            return
        requester = guild.get_member(stored.requester_id)
        if requester is None:
            with suppress(discord.NotFound, discord.Forbidden, discord.HTTPException):
                requester = await guild.fetch_member(stored.requester_id)
        requester_name = requester.display_name if requester else str(stored.requester_id)
        avatar = getattr(getattr(requester, "display_avatar", None), "url", None)
        public_request = stored.as_legacy_request(
            source_channel_id=stored.public_channel_id,
            source_message_id=stored.public_message_id,
        )

        dm_delivered = False
        if requester is not None:
            try:
                await requester.send(
                    embed=build_requester_decision_embed(public_request, decision, ui=self.ui),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                dm_delivered = True
            except (discord.Forbidden, discord.HTTPException):
                dm_delivered = False
        await repository.mark_dm_delivery(stored.id, dm_delivered)

        public_channel = guild.get_channel(stored.public_channel_id)
        if isinstance(public_channel, discord.TextChannel):
            try:
                public_message = await public_channel.fetch_message(stored.public_message_id)
                await public_message.edit(
                    embed=build_public_decision_embed(
                        public_request,
                        decision,
                        requester_name=requester_name,
                        requester_avatar_url=str(avatar) if avatar else None,
                        ui=self.ui,
                    ),
                    view=(None if dm_delivered else RoleRequestResolutionView(self, registry=self.registry)),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as error:
                log_event(
                    LOGGER,
                    "warning",
                    "role_request_public_update_failed",
                    exc_info=error,
                    guild_id=stored.guild_id,
                    channel_id=stored.public_channel_id,
                    message_id=stored.public_message_id,
                    request_id=str(stored.id),
                    **exception_fields(error),
                )

        private_embed = build_decision_embed(
            public_request,
            decision,
            requester_name=requester_name,
            requester_avatar_url=str(avatar) if avatar else None,
            ui=self.ui,
        )
        private_embed.add_field(
            name="🔗 Solicitud pública",
            value=(
                f"[Abrir solicitud](https://discord.com/channels/{stored.guild_id}/"
                f"{stored.public_channel_id}/{stored.public_message_id})"
            ),
            inline=False,
        )
        for mirror in await repository.review_messages(stored.id):
            review_channel = guild.get_channel(mirror.channel_id)
            if not isinstance(review_channel, discord.TextChannel):
                continue
            try:
                review_message = await review_channel.fetch_message(mirror.message_id)
                await review_message.edit(
                    embed=private_embed,
                    view=RoleRequestDecisionView(
                        self,
                        registry=self.registry,
                        disabled=True,
                        include_partial=stored.kind is RoleRequestKind.NORMAL,
                        kind=stored.kind,
                    ),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as error:
                log_event(
                    LOGGER,
                    "warning",
                    "role_request_private_update_failed",
                    exc_info=error,
                    guild_id=stored.guild_id,
                    channel_id=mirror.channel_id,
                    message_id=mirror.message_id,
                    request_id=str(stored.id),
                    **exception_fields(error),
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

        bot_member = guild.me
        if request.kind is RoleRequestKind.RETIREMENT:
            retired_role = guild.get_role(self.retired_role_id) if self.retired_role_id is not None else None
            invalid_roles = (
                bot_member is None
                or retired_role is None
                or request.add_role_ids != (self.retired_role_id,)
                or self._role_is_protected(retired_role, bot_member)
            )
        else:
            requested_roles = tuple(
                guild.get_role(role_id) for role_id in (*request.add_role_ids, *request.remove_role_ids)
            )
            invalid_roles = (
                any(role is None for role in requested_roles)
                or bot_member is None
                or any(
                    self._role_is_protected(role, bot_member) for role in requested_roles if role is not None
                )
            )
        if invalid_roles:
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
            view=RoleRequestDecisionView(
                self,
                disabled=True,
                include_partial=request.kind is RoleRequestKind.NORMAL,
                kind=request.kind,
            ),
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


def build_ftt_entry_welcome_embed(
    member: discord.Member,
    options: RoleRequestFttWelcomeOptions,
    *,
    ftp_request_channel_id: int | None = None,
) -> discord.Embed:
    request_hint = (
        f"\n\nCuando necesites una ficha, utiliza `/solicitar-ficha` en "
        f"<#{ftp_request_channel_id}>."
        if ftp_request_channel_id is not None
        else ""
    )
    embed = discord.Embed(
        title="🎓 ¡Bienvenido al Field Training Program!",
        description=(
            "Aquí puedes coordinar salidas de patrullaje para realizar tus fichas del FTP."
            f"{request_hint}"
        ),
        color=discord.Color.gold(),
    )
    embed.set_author(
        name=member.display_name,
        icon_url=str(member.display_avatar.url),
    )
    embed.add_field(
        name="📘 Reglamento",
        value=f"[Leer reglamento del programa]({options.rules_url})",
        inline=False,
    )
    embed.add_field(
        name="📌 Revisa también",
        value=" · ".join(f"<#{channel_id}>" for channel_id in options.reference_channel_ids),
        inline=False,
    )
    return embed


class RoleRequestDecisionView(discord.ui.View):
    """Persistent, generic controls resolved by the source message ID."""

    def __init__(
        self,
        service: RoleRequestService,
        *,
        registry: RoleRequestServiceRegistry | None = None,
        disabled: bool = False,
        include_partial: bool = True,
        kind: RoleRequestKind = RoleRequestKind.NORMAL,
    ) -> None:
        super().__init__(timeout=None)
        self.service = service
        self.registry = registry
        ui = service.ui
        self.approve_all_button.label = (
            "Aprobar ingreso" if kind is RoleRequestKind.FTT_ENTRY else ui.approve_all_button
        )
        self.approve_partial_button.label = ui.approve_partial_button
        self.reject_button.label = ui.reject_button
        if not include_partial:
            self.remove_item(self.approve_partial_button)
        if disabled:
            for child in self.children:
                child.disabled = True

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        try:
            service = await self._service_for(interaction)
        except LookupError:
            await interaction.response.send_message(
                embed=self.service.build_alert_embed(self.service.ui.request_unavailable),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return False
        return await service.authorize_reviewer_interaction(interaction)

    async def _service_for(self, interaction: discord.Interaction) -> RoleRequestService:
        guild = interaction.guild
        if self.registry is None or guild is None:
            return self.service
        return await self.registry.get(guild.id)

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
        service = await self._service_for(interaction)
        await service.approve_all(interaction)

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
        service = await self._service_for(interaction)
        try:
            request, _stored = await service.resolve_interaction_request(interaction)
        except RoleRequestCodecError:
            await interaction.response.send_message(
                embed=service.build_alert_embed(service.ui.stale_request),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if request.kind is not RoleRequestKind.NORMAL:
            await interaction.response.send_message(
                embed=service.build_alert_embed(_partial_approval_blocked_message(request.kind)),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                embed=service.build_alert_embed(service.ui.request_unavailable),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await interaction.response.send_modal(PartialApprovalModal(service, request=request, guild=guild))

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
        service = await self._service_for(interaction)
        try:
            request, _stored = await service.resolve_interaction_request(interaction)
        except RoleRequestCodecError:
            await interaction.response.send_message(
                embed=service.build_alert_embed(service.ui.stale_request),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await interaction.response.send_modal(RejectRoleRequestModal(service, request))


class RoleRequestResolutionView(discord.ui.View):
    """Requester and authorized-reviewer fallback for a private decision."""

    def __init__(
        self,
        service: RoleRequestService,
        *,
        registry: RoleRequestServiceRegistry | None = None,
    ) -> None:
        super().__init__(timeout=None)
        self.service = service
        self.registry = registry

    async def _service_for(self, interaction: discord.Interaction) -> RoleRequestService:
        guild = interaction.guild
        if self.registry is None or guild is None:
            return self.service
        return await self.registry.get(guild.id)

    @discord.ui.button(
        label="Ver resolución",
        emoji="📩",
        style=discord.ButtonStyle.secondary,
        custom_id="role-request:v2:view-resolution",
    )
    async def view_resolution(
        self,
        _button: discord.ui.Button,
        interaction: discord.Interaction,
    ) -> None:
        try:
            service = await self._service_for(interaction)
        except LookupError:
            service = self.service
        repository = service.repository
        guild = interaction.guild
        channel = getattr(interaction, "channel", None)
        message = interaction.message
        if repository is None or guild is None or channel is None or message is None:
            await interaction.response.send_message(
                embed=service.build_alert_embed(service.ui.request_unavailable),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        stored = await repository.get_by_public_message(guild.id, channel.id, message.id)
        if (
            stored is None
            or not stored.status.terminal
            or stored.reviewer_id is None
            or stored.decided_at is None
        ):
            await interaction.response.send_message(
                embed=service.build_alert_embed(service.ui.request_unavailable),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        member = interaction.user
        is_requester = stored.requester_id == member.id
        is_authorized_reviewer = isinstance(member, discord.Member) and service.is_reviewer(member)
        if not is_requester and not is_authorized_reviewer:
            await interaction.response.send_message(
                embed=service.build_alert_embed(service.ui.reviewer_unauthorized),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        request = stored.as_legacy_request(
            source_channel_id=stored.public_channel_id,
            source_message_id=stored.public_message_id or message.id,
        )
        decision = RoleRequestDecision(
            status=RoleRequestStatus(stored.status.value),
            reviewer_id=stored.reviewer_id,
            approved_add_role_ids=stored.approved_add_role_ids,
            approved_remove_role_ids=stored.approved_remove_role_ids,
            unapproved_add_role_ids=stored.unapproved_add_role_ids,
            unapproved_remove_role_ids=stored.unapproved_remove_role_ids,
            comment=stored.decision_comment,
            decided_at=stored.decided_at,
        )
        await interaction.response.send_message(
            embed=build_requester_decision_embed(request, decision, ui=service.ui),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class RoleRequestStartView(discord.ui.View):
    """Private entry choice for normal, guided-entry and retirement requests."""

    def __init__(
        self,
        service: RoleRequestService,
        *,
        member: discord.Member,
        channel: discord.TextChannel,
    ) -> None:
        super().__init__(timeout=MODAL_TIMEOUT_SECONDS)
        self.service = service
        self.member = member
        self.channel = channel
        self.normal_button.label = ROLE_REQUEST_MODAL_COPY.normal_button
        self.ftt_entry_button.label = ROLE_REQUEST_MODAL_COPY.ftt_entry_button
        self.retirement_button.label = ROLE_REQUEST_MODAL_COPY.retirement_button
        if not service.ftt_entry_enabled:
            self.remove_item(self.ftt_entry_button)
        if not service.retirement_enabled:
            self.remove_item(self.retirement_button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.member.id:
            return True
        await interaction.response.send_message(
            embed=self.service.build_alert_embed(self.service.ui.member_unavailable),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return False

    @discord.ui.button(label="Cambiar roles", emoji="🔄", style=discord.ButtonStyle.primary)
    async def normal_button(
        self,
        _button: discord.ui.Button,
        interaction: discord.Interaction,
    ) -> None:
        try:
            modal = CreateRoleRequestModal(
                self.service,
                member=self.member,
                channel=self.channel,
            )
        except RoleRequestValidationError as error:
            await interaction.response.send_message(
                embed=self.service.build_alert_embed(error.public_message),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Ingreso FTT", emoji="🎓", style=discord.ButtonStyle.success)
    async def ftt_entry_button(
        self,
        _button: discord.ui.Button,
        interaction: discord.Interaction,
    ) -> None:
        try:
            modal = FttEntryRoleRequestModal(
                self.service,
                member=self.member,
                channel=self.channel,
            )
        except RoleRequestValidationError as error:
            await interaction.response.send_message(
                embed=self.service.build_alert_embed(error.public_message),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="Solicitar retiro", emoji="📤", style=discord.ButtonStyle.danger)
    async def retirement_button(
        self,
        _button: discord.ui.Button,
        interaction: discord.Interaction,
    ) -> None:
        try:
            self.service.retirement_changes(self.member)
        except RoleRequestValidationError as error:
            await interaction.response.send_message(
                embed=self.service.build_alert_embed(error.public_message),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await interaction.response.send_modal(
            RetirementRoleRequestModal(
                self.service,
                member=self.member,
                channel=self.channel,
            )
        )


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
        children.extend(_labeled_role_selects(copy.add_roles_label, self.add_selects))
        children.extend(_labeled_role_selects(copy.remove_roles_label, self.remove_selects))
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


class FttEntryRoleRequestModal(discord.ui.DesignerModal):
    """Reduced entry form where the member chooses only a destination station."""

    def __init__(
        self,
        service: RoleRequestService,
        *,
        member: discord.Member,
        channel: discord.TextChannel,
    ) -> None:
        options = service.ftt_entry_options
        if options is None:
            raise RoleRequestValidationError(service.ui.request_unavailable)
        self.service = service
        self.member = member
        self.channel = channel
        copy = ROLE_REQUEST_MODAL_COPY
        self.station_select = discord.ui.StringSelect(
            custom_id="role-request:v1:ftt-entry:station",
            options=[
                discord.SelectOption(
                    label=station.label,
                    value=station.key,
                    emoji=station.emoji,
                )
                for station in options.stations
            ],
            placeholder=copy.ftt_entry_station_placeholder,
            min_values=1,
            max_values=1,
            required=True,
        )
        super().__init__(
            discord.ui.Label(copy.ftt_entry_station_label, self.station_select),
            title=copy.ftt_entry_title,
            custom_id=f"role-request:v1:ftt-entry:{member.id}",
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
        station_key = self.station_select.values[0] if self.station_select.values else ""
        await self.service.create_ftt_entry_request(
            interaction,
            member=member,
            channel=self.channel,
            station_key=station_key,
        )


class RetirementRoleRequestModal(discord.ui.DesignerModal):
    """Reduced form: retirement derives all managed removals server-side."""

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
        self.reason_input = discord.ui.InputText(
            custom_id="role-request:v1:retirement:reason",
            style=discord.InputTextStyle.long,
            placeholder=copy.retirement_reason_placeholder,
            required=False,
            max_length=MAX_REASON_LENGTH,
        )
        self.confirm = discord.ui.Checkbox(
            custom_id="role-request:v1:retirement:confirm",
            default=False,
        )
        super().__init__(
            discord.ui.Label(copy.retirement_reason_label, self.reason_input),
            discord.ui.Label(copy.retirement_confirm_label, self.confirm),
            title=copy.retirement_title,
            custom_id=f"role-request:v1:retirement:{member.id}",
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
        if not self.confirm.value:
            await interaction.response.send_message(
                embed=self.service.build_alert_embed(
                    ROLE_REQUEST_MODAL_COPY.retirement_confirmation_required
                ),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await self.service.create_retirement_request(
            interaction,
            member=member,
            channel=self.channel,
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
            required=True,
            min_length=1,
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
            selected_add=tuple(int(value) for value in self.add_select.values) if self.add_select else (),
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

    def __init__(
        self,
        service: RoleRequestService,
        *,
        registry: RoleRequestServiceRegistry | None = None,
    ) -> None:
        self.service = service
        self.registry = registry
        command_copy = service.persona.messages.interface.commands
        self.request_roles.description = command_copy.request_roles_description
        self.info_roles.description = command_copy.info_roles_description

    async def _service_for(self, guild: discord.Guild | None) -> RoleRequestService:
        if self.registry is None or guild is None:
            return self.service
        return await self.registry.get(guild.id)

    @discord.slash_command(
        name="solicitar-roles",
        description="Solicita cambios de roles para aprobación",
    )
    async def request_roles(self, ctx: discord.ApplicationContext) -> None:
        guild = ctx.guild
        member = ctx.author
        channel = ctx.channel
        try:
            service = await self._service_for(guild)
        except LookupError:
            await ctx.respond(
                embed=self.service.build_alert_embed(self.service.ui.wrong_guild),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        ui = service.ui
        if guild is None or not service.settings.guild_allowlist.allows(guild.id):
            await ctx.respond(
                embed=service.build_alert_embed(ui.wrong_guild),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if not isinstance(member, discord.Member):
            await ctx.respond(
                embed=service.build_alert_embed(ui.member_unavailable),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if isinstance(channel, discord.Thread):
            await ctx.respond(
                embed=service.build_alert_embed(ui.thread_blocked),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if not isinstance(channel, discord.TextChannel):
            await ctx.respond(
                embed=service.build_alert_embed(ui.wrong_channel.format(channels=ui.no_available_channels)),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if not service.channel_is_allowed(channel):
            channels = await service.allowed_channel_summary(guild)
            await ctx.respond(
                embed=service.build_alert_embed(ui.wrong_channel.format(channels=channels)),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        try:
            service.validate_channel_permissions(guild, channel)
        except RoleRequestValidationError as error:
            await ctx.respond(
                embed=service.build_alert_embed(error.public_message),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        try:
            if service.start_view_enabled:
                view = RoleRequestStartView(
                    service,
                    member=member,
                    channel=channel,
                )
            else:
                modal = CreateRoleRequestModal(
                    service,
                    member=member,
                    channel=channel,
                )
        except RoleRequestValidationError as error:
            await ctx.respond(
                embed=service.build_alert_embed(error.public_message),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if service.start_view_enabled:
            await ctx.respond(
                embed=service.build_notice_embed(
                    title=ROLE_REQUEST_MODAL_COPY.start_title,
                    description=ROLE_REQUEST_MODAL_COPY.start_description,
                    color=discord.Color.blurple(),
                ),
                view=view,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        else:
            await ctx.send_modal(modal)

    @discord.slash_command(
        name="info-roles",
        description="Explica cómo solicitar y aprobar cambios de roles",
    )
    async def info_roles(self, ctx: discord.ApplicationContext) -> None:
        guild = ctx.guild
        try:
            service = await self._service_for(guild)
        except LookupError:
            await ctx.respond(
                embed=self.service.build_alert_embed(self.service.ui.wrong_guild),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        ui = service.ui
        if guild is None or not service.settings.guild_allowlist.allows(guild.id):
            await ctx.respond(
                embed=service.build_alert_embed(ui.wrong_guild),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        channels = await service.allowed_channel_summary(guild)
        embed = discord.Embed(
            title=ui.info_title,
            description=ui.info_description,
            color=discord.Color.blurple(),
        )
        embed.add_field(name=ui.info_usage_title, value=ui.info_usage, inline=False)
        embed.add_field(name=ui.info_rules_title, value=ui.info_rules, inline=False)
        embed.add_field(
            name="🔒 Privacidad de la revisión",
            value=(
                "El canal conserva el estado de la solicitud. El motivo y los comentarios "
                "de revisión se tratan en espacios privados; la resolución te llegará por MD."
            ),
            inline=False,
        )
        if service.retirement_enabled:
            embed.add_field(
                name="📤 Retiro de la facción",
                value=(
                    "Elige **Solicitar retiro** al abrir `/solicitar-roles`. "
                    "No tendrás que indicar bajas: al aprobarse se conservará únicamente "
                    "el rol de retiro dentro del catálogo gestionado."
                ),
                inline=False,
            )
        if service.ftt_entry_enabled:
            embed.add_field(
                name="🎓 Ingreso FTT",
                value=(
                    "Elige **Ingreso FTT**, selecciona tu estación y el bot preparará "
                    "automáticamente todos los roles iniciales necesarios."
                ),
                inline=False,
            )
        embed.add_field(name=ui.info_channels_title, value=channels, inline=False)
        embed.add_field(name=ui.info_outcomes_title, value=ui.info_outcomes, inline=False)
        avatar = getattr(getattr(service.bot.user, "display_avatar", None), "url", None)
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
                label=(role.name if role else unknown_role_label.format(role_id=role_id))[:100],
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
        options = [discord.SelectOption(label=role.name[:100], value=str(role.id)) for role in group]
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


def _role_select_count(roles: tuple[discord.Role, ...]) -> int:
    if not roles:
        return 0
    return (len(roles) + MAX_STRING_SELECT_OPTIONS - 1) // MAX_STRING_SELECT_OPTIONS


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

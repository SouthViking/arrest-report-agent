"""Discord UI for deterministic FTP assistance requests."""

from __future__ import annotations

import asyncio
import logging
import math
from contextlib import suppress
from datetime import UTC, datetime
from typing import cast
from uuid import UUID
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands

from arrest_report_agent.config import BotPersona, FtpRequestOptions, GuildRuntimeSettings
from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.infrastructure.postgres import DatabaseConfigProvider

from .coordination import build_coordination_embed
from .models import (
    FtpInstructorLevel,
    FtpRequestStatus,
    FtpTrainingPhase,
    FtpTrainingRequest,
)
from .repository import FtpRequestRepository

LOGGER = logging.getLogger(__name__)
MODAL_TIMEOUT_SECONDS = 300
LIST_TIMEOUT_SECONDS = 900
ACTIVE_REQUEST_PAGE_SIZE = 10
FTP_TIMEZONE_OPTIONS = (
    ("🇦🇷", "America/Argentina/Buenos_Aires"),
    ("🇨🇱", "America/Santiago"),
    ("🇨🇴", "America/Bogota"),
    ("🇻🇪", "America/Caracas"),
    ("🇪🇸", "Europe/Madrid"),
    ("🇪🇸", "Atlantic/Canary"),
    ("🇲🇽", "America/Mexico_City"),
    ("🇲🇽", "America/Mazatlan"),
    ("🇲🇽", "America/Hermosillo"),
    ("🇲🇽", "America/Cancun"),
    ("🇲🇽", "America/Tijuana"),
)
FTP_TIMEZONE_KEYS = frozenset(timezone_key for _, timezone_key in FTP_TIMEZONE_OPTIONS)


class FtpRequestServiceRegistry:
    """Resolve a service from the guild's current immutable configuration."""

    def __init__(
        self,
        bot: discord.Bot,
        provider: DatabaseConfigProvider,
        repository: FtpRequestRepository,
    ) -> None:
        self.bot = bot
        self.provider = provider
        self.repository = repository
        self._services: dict[UUID, FtpRequestService] = {}

    async def get(
        self,
        guild_id: int,
        *,
        require_enabled: bool = True,
    ) -> FtpRequestService:
        bundle = await self.provider.cached(guild_id)
        feature = bundle.snapshot.features.get("ftp_requests")
        enabled = bool(feature and feature.enabled)
        if require_enabled and not enabled:
            raise LookupError(f"FTP requests are disabled for guild {guild_id}")
        service = self._services.get(bundle.snapshot.revision_id)
        if service is None:
            service = FtpRequestService(
                self.bot,
                self.repository,
                bundle.settings,
                bundle.persona.profile,
                enabled=enabled,
            )
            self._services[bundle.snapshot.revision_id] = service
        return service


class FtpRequestService:
    def __init__(
        self,
        bot: discord.Bot,
        repository: FtpRequestRepository,
        settings: GuildRuntimeSettings,
        persona: BotPersona,
        *,
        enabled: bool,
    ) -> None:
        self.bot = bot
        self.repository = repository
        self.settings = settings
        self.ui = persona.messages.interface.ftp_requests
        self.enabled = enabled

    @property
    def request_options(self) -> FtpRequestOptions | None:
        return self.settings.ftp_request_options

    def alert(self, description: str) -> discord.Embed:
        return discord.Embed(
            title=self.ui.alert_title,
            description=description,
            color=discord.Color.red(),
        )

    def can_request(self, member: discord.Member) -> bool:
        ftp = self.settings.ftp_status_options
        options = self.request_options
        if ftp is None or options is None:
            return False
        return _has_any_role(member, (*ftp.ftt_role_ids, *options.management_role_ids))

    def can_list_active(self, member: discord.Member) -> bool:
        return _has_any_role(member, self.settings.operational_staff_role_ids)

    def is_command_channel(self, channel_id: int) -> bool:
        options = self.request_options
        return bool(options and channel_id in options.command_channel_ids)

    def command_channel_mentions(self) -> str:
        options = self.request_options
        if options is None:
            return ""
        return " o ".join(f"<#{channel_id}>" for channel_id in options.command_channel_ids)

    def can_claim(self, member: discord.Member, request: FtpTrainingRequest) -> bool:
        ftp = self.settings.ftp_status_options
        options = self.request_options
        if ftp is None or options is None:
            return False
        level = request.requested_instructor_level
        if level is FtpInstructorLevel.SUPERVISOR:
            eligible = (*ftp.supervisory_role_ids, *options.management_role_ids)
        else:
            # Legacy and general requests can be claimed by any qualified instructor.
            eligible = (
                *ftp.supervisory_role_ids,
                *ftp.mfto_role_ids,
                *ftp.fto_role_ids,
                *options.management_role_ids,
            )
        return _has_any_role(member, eligible)

    def can_cancel(self, member: discord.Member, request: FtpTrainingRequest) -> bool:
        options = self.request_options
        return member.id == request.requester_user_id or bool(
            options and _has_any_role(member, options.management_role_ids)
        )

    async def destination_channel(self, guild: discord.Guild) -> discord.TextChannel:
        options = self.request_options
        if options is None:
            raise LookupError("FTP request destination is not configured")
        resolved_channel: object | None = guild.get_channel(options.channel_id)
        if resolved_channel is None:
            resolved_channel = await guild.fetch_channel(options.channel_id)
        if not isinstance(resolved_channel, discord.TextChannel):
            raise LookupError("FTP request destination is not a text channel")
        bot_member = guild.me
        if bot_member is None:
            raise LookupError("Bot guild member is unavailable")
        permissions = resolved_channel.permissions_for(bot_member)
        if not permissions.send_messages or not permissions.embed_links:
            raise PermissionError("Bot cannot publish FTP requests in the configured channel")
        return resolved_channel

    async def ensure_request_panel(self, guild: discord.Guild) -> discord.Message:
        channel = await self.destination_channel(guild)
        panel = await self.repository.get_panel(guild.id)
        message: discord.Message | None = None
        if panel is not None and panel.channel_id == channel.id:
            try:
                message = await channel.fetch_message(panel.message_id)
            except discord.NotFound:
                message = None
        if message is None:
            message = await channel.send(
                embed=build_ftp_request_panel_embed(self),
                view=FtpRequestPanelView(),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            try:
                await self.repository.save_panel(
                    guild_id=guild.id,
                    channel_id=channel.id,
                    message_id=message.id,
                )
            except Exception:
                with suppress(discord.HTTPException):
                    await message.delete()
                raise
        else:
            await message.edit(
                embed=build_ftp_request_panel_embed(self),
                view=FtpRequestPanelView(),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        if not message.pinned:
            await message.pin(reason="Panel de solicitudes de fichas FTP")
        return message

    def notification_roles(
        self,
        guild: discord.Guild,
        level: FtpInstructorLevel,
    ) -> tuple[discord.Role, ...]:
        ftp = self.settings.ftp_status_options
        if ftp is None:
            return ()
        role_ids = (
            ftp.supervisory_role_ids
            if level is FtpInstructorLevel.SUPERVISOR
            else (*ftp.fto_role_ids, *ftp.mfto_role_ids, *ftp.supervisory_role_ids)
        )
        return tuple(role for role_id in role_ids if (role := guild.get_role(role_id)) is not None)

    async def create(
        self,
        interaction: discord.Interaction,
        *,
        member: discord.Member,
        training_phase: FtpTrainingPhase,
        requested_instructor_level: FtpInstructorLevel,
        scheduled_for: datetime,
        comments: str = "",
    ) -> None:
        if scheduled_for.tzinfo is None:
            await interaction.response.send_message(
                embed=self.alert(self.ui.invalid_schedule),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await interaction.response.defer(ephemeral=True)
        request: FtpTrainingRequest | None = None
        message: discord.Message | None = None
        try:
            channel = await self.destination_channel(member.guild)
            roles = self.notification_roles(member.guild, requested_instructor_level)
            if not roles:
                raise LookupError("No FTP notification roles are available")
            request = await self.repository.create_publishing(
                guild_id=member.guild.id,
                channel_id=channel.id,
                requester_user_id=member.id,
                training_phase=training_phase,
                requested_instructor_level=requested_instructor_level,
                scheduled_for=scheduled_for,
                reason=comments,
            )
            message = await channel.send(
                content=" ".join(role.mention for role in roles),
                embed=build_ftp_request_embed(request, requester=member),
                view=FtpRequestControlsView(),
                allowed_mentions=discord.AllowedMentions(
                    everyone=False,
                    users=False,
                    roles=list(roles),
                    replied_user=False,
                ),
            )
            request = await self.repository.publish(request.id, message.id)
            await message.edit(
                embed=build_ftp_request_embed(request, requester=member),
                view=FtpRequestControlsView(),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            log_event(
                LOGGER,
                "info",
                "ftp_request_published",
                guild_id=request.guild_id,
                channel_id=request.channel_id,
                message_id=request.message_id,
                requester_user_id=request.requester_user_id,
            )
        except Exception as error:
            if request is not None:
                with suppress(Exception):
                    await self.repository.mark_failed(request.id)
            if message is not None:
                with suppress(discord.HTTPException):
                    await message.delete()
            await interaction.followup.send(
                embed=self.alert(self.ui.operation_failed),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            self._log_error("ftp_request_publish_failed", error, interaction)

    async def resolve(self, interaction: discord.Interaction) -> FtpTrainingRequest:
        if interaction.guild_id is None or interaction.channel_id is None or interaction.message is None:
            raise LookupError("FTP request interaction has no message context")
        request = await self.repository.get_by_message(
            interaction.guild_id,
            interaction.channel_id,
            interaction.message.id,
        )
        if request is None:
            raise LookupError("FTP request message is not registered")
        return request

    async def claim(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        try:
            request = await self.resolve(interaction)
        except LookupError:
            await interaction.response.send_message(embed=self.alert(self.ui.stale), ephemeral=True)
            return
        if not isinstance(member, discord.Member) or not self.can_claim(member, request):
            await interaction.response.send_message(
                embed=self.alert(self.ui.unauthorized_acceptor), ephemeral=True
            )
            return
        if request.status is not FtpRequestStatus.PENDING:
            await interaction.response.send_message(embed=self.alert(self.ui.stale), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        changed = False
        try:
            request, changed = await self.repository.claim(request.id, actor_user_id=member.id)
            if not changed:
                await interaction.followup.send(embed=self.alert(self.ui.stale), ephemeral=True)
                return
            await self._edit_interaction_message(interaction, request)
            self._log_transition("ftp_request_claimed", request, member.id)
        except Exception as error:
            if changed:
                with suppress(Exception):
                    await self.repository.restore_pending(
                        request.id,
                        expected_status=FtpRequestStatus.CLAIMED,
                    )
            await interaction.followup.send(embed=self.alert(self.ui.operation_failed), ephemeral=True)
            self._log_error("ftp_request_claim_failed", error, interaction)
            return
        await self._notify_requester(interaction, request, member)

    async def cancel(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        try:
            request = await self.resolve(interaction)
        except LookupError:
            await interaction.response.send_message(embed=self.alert(self.ui.stale), ephemeral=True)
            return
        if not isinstance(member, discord.Member) or not self.can_cancel(member, request):
            await interaction.response.send_message(
                embed=self.alert(self.ui.unauthorized_canceller), ephemeral=True
            )
            return
        if request.status is not FtpRequestStatus.PENDING:
            await interaction.response.send_message(embed=self.alert(self.ui.stale), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        changed = False
        try:
            request, changed = await self.repository.cancel(request.id, actor_user_id=member.id)
            if not changed:
                await interaction.followup.send(embed=self.alert(self.ui.stale), ephemeral=True)
                return
            await self._edit_interaction_message(interaction, request)
            self._log_transition("ftp_request_cancelled", request, member.id)
        except Exception as error:
            if changed:
                with suppress(Exception):
                    await self.repository.restore_pending(
                        request.id,
                        expected_status=FtpRequestStatus.CANCELLED,
                    )
            await interaction.followup.send(embed=self.alert(self.ui.operation_failed), ephemeral=True)
            self._log_error("ftp_request_cancel_failed", error, interaction)
            return

    async def _edit_interaction_message(
        self,
        interaction: discord.Interaction,
        request: FtpTrainingRequest,
    ) -> None:
        if interaction.message is None:
            raise LookupError("FTP request Discord message is unavailable")
        requester = await self._requester_member(interaction, request.requester_user_id)
        await interaction.message.edit(
            content=None,
            embed=build_ftp_request_embed(request, requester=requester),
            view=FtpRequestControlsView(disabled=True),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def _notify_requester(
        self,
        interaction: discord.Interaction,
        request: FtpTrainingRequest,
        instructor: discord.Member,
    ) -> None:
        if interaction.message is None:
            return
        requester = discord.Object(id=request.requester_user_id)
        try:
            await interaction.message.reply(
                content=f"<@{request.requester_user_id}> <@{instructor.id}>",
                embed=build_coordination_embed(
                    trainee_id=request.requester_user_id,
                    instructor_id=instructor.id,
                    phase=request.training_phase,
                    starts_at=request.scheduled_for,
                    comments=request.reason,
                ),
                allowed_mentions=discord.AllowedMentions(
                    everyone=False,
                    users=[requester, discord.Object(id=instructor.id)],
                    roles=False,
                    replied_user=False,
                ),
            )
        except (discord.Forbidden, discord.HTTPException) as error:
            log_event(
                LOGGER,
                "warning",
                "ftp_request_requester_notification_failed",
                exc_info=error,
                guild_id=request.guild_id,
                channel_id=request.channel_id,
                message_id=request.message_id,
                requester_user_id=request.requester_user_id,
                actor_user_id=instructor.id,
                **exception_fields(error),
            )

    @staticmethod
    async def _requester_member(
        interaction: discord.Interaction,
        requester_user_id: int,
    ) -> discord.Member | None:
        guild = interaction.guild
        if guild is None:
            return None
        member = guild.get_member(requester_user_id)
        if member is not None:
            return member
        try:
            return await guild.fetch_member(requester_user_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None

    @staticmethod
    def _log_transition(event: str, request: FtpTrainingRequest, actor_user_id: int) -> None:
        log_event(
            LOGGER,
            "info",
            event,
            guild_id=request.guild_id,
            channel_id=request.channel_id,
            message_id=request.message_id,
            requester_user_id=request.requester_user_id,
            actor_user_id=actor_user_id,
            status=request.status.value,
        )

    @staticmethod
    def _log_error(event: str, error: Exception, interaction: discord.Interaction) -> None:
        log_event(
            LOGGER,
            "error",
            event,
            exc_info=error,
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            user_id=getattr(interaction.user, "id", None),
            **exception_fields(error),
        )


class FtpRequestControlsView(discord.ui.View):
    def __init__(self, *, disabled: bool = False) -> None:
        super().__init__(timeout=None)
        if disabled:
            for child in self.children:
                cast("discord.ui.Button[FtpRequestControlsView]", child).disabled = True

    async def _service(self, interaction: discord.Interaction) -> FtpRequestService | None:
        registry: FtpRequestServiceRegistry = interaction.client.ftp_request_registry  # type: ignore[attr-defined]
        if interaction.guild_id is None:
            return None
        try:
            return await registry.get(interaction.guild_id, require_enabled=False)
        except LookupError:
            return None

    @discord.ui.button(
        label="Tomar ficha",
        emoji="🙋",
        style=discord.ButtonStyle.success,
        custom_id="ftp-request:v1:claim",
    )
    async def claim_button(
        self,
        _button: discord.ui.Button[FtpRequestControlsView],
        interaction: discord.Interaction,
    ) -> None:
        service = await self._service(interaction)
        if service is None or not service.enabled:
            await interaction.response.send_message(embed=_unavailable_embed(service), ephemeral=True)
            return
        await service.claim(interaction)

    @discord.ui.button(
        label="Cancelar",
        emoji="✖️",
        style=discord.ButtonStyle.danger,
        custom_id="ftp-request:v1:cancel",
    )
    async def cancel_button(
        self,
        _button: discord.ui.Button[FtpRequestControlsView],
        interaction: discord.Interaction,
    ) -> None:
        service = await self._service(interaction)
        if service is None or not service.enabled:
            await interaction.response.send_message(embed=_unavailable_embed(service), ephemeral=True)
            return
        await service.cancel(interaction)


class FtpTimezoneSelect(discord.ui.Select["FtpTimezoneView"]):
    def __init__(self) -> None:
        super().__init__(
            custom_id="ftp-request:v1:timezone",
            placeholder="Selecciona tu zona horaria",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label=timezone_key, value=timezone_key, emoji=emoji)
                for emoji, timezone_key in FTP_TIMEZONE_OPTIONS
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = cast(FtpTimezoneView, self.view)
        await view.open_modal(interaction, next(iter(self.values or ()), ""))


class FtpTimezoneView(discord.ui.View):
    def __init__(self, service: FtpRequestService, member: discord.Member) -> None:
        super().__init__(timeout=MODAL_TIMEOUT_SECONDS, disable_on_timeout=True)
        self.service = service
        self.member = member
        self.requester_id = member.id
        self.add_item(FtpTimezoneSelect())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if getattr(interaction.user, "id", None) == self.requester_id:
            return True
        await interaction.response.send_message(
            embed=self.service.alert(self.service.ui.unauthorized_requester),
            ephemeral=True,
        )
        return False

    async def open_modal(self, interaction: discord.Interaction, timezone_key: str) -> None:
        if timezone_key not in FTP_TIMEZONE_KEYS:
            await interaction.response.send_message(
                embed=self.service.alert(self.service.ui.invalid_schedule),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        self.stop()
        await interaction.response.send_modal(
            FtpRequestModal(self.service, self.member, timezone_key)
        )


class FtpRequestPanelView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Solicitar ficha",
        emoji="🎓",
        style=discord.ButtonStyle.primary,
        custom_id="ftp-request:v1:open-form",
    )
    async def open_form_button(
        self,
        _button: discord.ui.Button[FtpRequestPanelView],
        interaction: discord.Interaction,
    ) -> None:
        registry: FtpRequestServiceRegistry = interaction.client.ftp_request_registry  # type: ignore[attr-defined]
        if interaction.guild_id is None:
            await interaction.response.send_message(
                embed=_unavailable_embed(None),
                ephemeral=True,
            )
            return
        try:
            service = await registry.get(interaction.guild_id, require_enabled=False)
        except LookupError:
            service = None
        if service is None or not service.enabled or service.request_options is None:
            await interaction.response.send_message(
                embed=_unavailable_embed(service),
                ephemeral=True,
            )
            return
        member = interaction.user
        if not isinstance(member, discord.Member) or not service.can_request(member):
            await interaction.response.send_message(
                embed=service.alert(service.ui.unauthorized_requester),
                ephemeral=True,
            )
            return
        if interaction.channel_id != service.request_options.channel_id:
            channel_mention = f"<#{service.request_options.channel_id}>"
            await interaction.response.send_message(
                embed=service.alert(service.ui.wrong_channel.format(channel=channel_mention)),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await interaction.response.send_message(
            "Selecciona la zona horaria que usarás para esta solicitud.",
            view=FtpTimezoneView(service, member),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    @discord.ui.button(
        label="Ofrecer disponibilidad", emoji="👨‍🏫", style=discord.ButtonStyle.secondary,
        custom_id="ftp-availability:v1:open-form", row=1,
    )
    async def offer_form_button(
        self, _button: discord.ui.Button[FtpRequestPanelView], interaction: discord.Interaction,
    ) -> None:
        from .availability_discord import availability_service

        await availability_service(interaction).open_form(interaction)


class FtpActiveRequestsView(discord.ui.View):
    def __init__(self, service: FtpRequestService, requester_id: int) -> None:
        super().__init__(timeout=LIST_TIMEOUT_SECONDS)
        self.service = service
        self.requester_id = requester_id
        self.page = 0
        self.total = 0
        self._sync_buttons()

    async def render(self, guild_id: int) -> discord.Embed:
        entries, self.total = await self.service.repository.list_pending(
            guild_id,
            offset=self.page * ACTIVE_REQUEST_PAGE_SIZE,
            limit=ACTIVE_REQUEST_PAGE_SIZE,
        )
        last_page = max(0, math.ceil(self.total / ACTIVE_REQUEST_PAGE_SIZE) - 1)
        if self.page > last_page:
            self.page = last_page
            entries, self.total = await self.service.repository.list_pending(
                guild_id,
                offset=self.page * ACTIVE_REQUEST_PAGE_SIZE,
                limit=ACTIVE_REQUEST_PAGE_SIZE,
            )
        self._sync_buttons()
        return build_active_ftp_requests_embed(entries, self.total, self.page)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if getattr(interaction.user, "id", None) == self.requester_id:
            return True
        await interaction.response.send_message(
            "Solo quien abrió esta consulta puede cambiar de página.",
            ephemeral=True,
        )
        return False

    @discord.ui.button(label="Anterior", emoji="◀️", style=discord.ButtonStyle.secondary)
    async def previous(
        self,
        _button: discord.ui.Button[FtpActiveRequestsView],
        interaction: discord.Interaction,
    ) -> None:
        self.page = max(0, self.page - 1)
        guild_id = interaction.guild_id
        if guild_id is None:
            await interaction.response.send_message(
                "Esta consulta solo funciona dentro de un servidor.",
                ephemeral=True,
            )
            return
        await interaction.response.edit_message(
            embed=await self.render(guild_id),
            view=self,
        )

    @discord.ui.button(label="Siguiente", emoji="▶️", style=discord.ButtonStyle.secondary)
    async def next(
        self,
        _button: discord.ui.Button[FtpActiveRequestsView],
        interaction: discord.Interaction,
    ) -> None:
        pages = max(1, math.ceil(self.total / ACTIVE_REQUEST_PAGE_SIZE))
        self.page = min(pages - 1, self.page + 1)
        guild_id = interaction.guild_id
        if guild_id is None:
            await interaction.response.send_message(
                "Esta consulta solo funciona dentro de un servidor.",
                ephemeral=True,
            )
            return
        await interaction.response.edit_message(
            embed=await self.render(guild_id),
            view=self,
        )

    def _sync_buttons(self) -> None:
        pages = max(1, math.ceil(self.total / ACTIVE_REQUEST_PAGE_SIZE))
        self.previous.disabled = self.page <= 0
        self.next.disabled = self.page >= pages - 1


class FtpRequestModal(discord.ui.DesignerModal):
    def __init__(
        self,
        service: FtpRequestService,
        member: discord.Member,
        timezone_key: str,
    ) -> None:
        if timezone_key not in FTP_TIMEZONE_KEYS:
            raise ValueError("Unsupported FTP request timezone")
        self.service = service
        self.member_id = member.id
        self.timezone_key = timezone_key
        self.training_phase = discord.ui.StringSelect(  # type: ignore[type-var]
            custom_id="ftp-request:v2:create:phase",
            placeholder="Seleccione la ficha que realizará",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label="Fase 1", value=FtpTrainingPhase.PHASE_1.value, emoji="1️⃣"),
                discord.SelectOption(label="Fase 2", value=FtpTrainingPhase.PHASE_2.value, emoji="2️⃣"),
                discord.SelectOption(label="Fase 3", value=FtpTrainingPhase.PHASE_3.value, emoji="3️⃣"),
            ],
        )
        self.instructor_level = discord.ui.StringSelect(  # type: ignore[type-var]
            custom_id="ftp-request:v2:create:instructor",
            placeholder="Seleccione el tipo de evaluador",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label="General",
                    value=FtpInstructorLevel.GENERAL.value,
                    description="Cualquier instructor disponible",
                    emoji="👨‍🏫",
                ),
                discord.SelectOption(
                    label="Supervisor",
                    value=FtpInstructorLevel.SUPERVISOR.value,
                    description="Supervisory, Command o Executive",
                    emoji="⭐",
                ),
            ],
        )
        self.date = discord.ui.InputText(
            custom_id="ftp-request:v2:create:date",
            placeholder="DD/MM/AAAA",
            min_length=10,
            max_length=10,
        )
        self.time = discord.ui.InputText(
            custom_id="ftp-request:v2:create:time",
            placeholder="HH:MM",
            min_length=4,
            max_length=5,
        )
        self.comments = discord.ui.InputText(
            custom_id="ftp-request:v2:create:comments",
            style=discord.InputTextStyle.long,
            placeholder="Indicaciones adicionales para el evaluador",
            required=False,
            max_length=500,
        )
        super().__init__(
            discord.ui.Label("Ficha a realizar", self.training_phase),
            discord.ui.Label("Tipo de evaluador", self.instructor_level),
            discord.ui.Label("Fecha", self.date),
            discord.ui.Label(f"Hora · {timezone_key}", self.time),
            discord.ui.Label("Comentarios (opcional)", self.comments),
            title="Solicitar una ficha FTP",
            custom_id=f"ftp-request:v2:create:{member.id}",
            timeout=MODAL_TIMEOUT_SECONDS,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member) or member.id != self.member_id:
            await interaction.response.send_message(
                embed=self.service.alert(self.service.ui.unauthorized_requester),
                ephemeral=True,
            )
            return
        try:
            training_phase = FtpTrainingPhase(next(iter(self.training_phase.values or ())))
            instructor_level = FtpInstructorLevel(next(iter(self.instructor_level.values or ())))
            scheduled_for = parse_ftp_schedule(
                self.date.value or "",
                self.time.value or "",
                self.timezone_key,
            )
        except (StopIteration, ValueError):
            await interaction.response.send_message(
                embed=self.service.alert(self.service.ui.invalid_schedule),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await self.service.create(
            interaction,
            member=member,
            training_phase=training_phase,
            requested_instructor_level=instructor_level,
            scheduled_for=scheduled_for,
            comments=self.comments.value or "",
        )


class FtpRequestCog(commands.Cog):
    def __init__(self, registry: FtpRequestServiceRegistry, persona: BotPersona) -> None:
        self.registry = registry
        self._panel_lock = asyncio.Lock()
        self.request_training.description = persona.messages.interface.commands.ftp_request_description
        self.active_requests.description = (
            persona.messages.interface.commands.active_ftp_requests_description
        )
        self.info_ftp.description = persona.messages.interface.commands.info_ftp_description

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        async with self._panel_lock:
            for guild in self.registry.bot.guilds:
                try:
                    service = await self.registry.get(guild.id, require_enabled=False)
                    if not service.enabled or service.request_options is None:
                        continue
                    message = await service.ensure_request_panel(guild)
                    log_event(
                        LOGGER,
                        "info",
                        "ftp_request_panel_ready",
                        guild_id=guild.id,
                        channel_id=message.channel.id,
                        message_id=message.id,
                        pinned=True,
                    )
                except Exception as error:
                    log_event(
                        LOGGER,
                        "error",
                        "ftp_request_panel_failed",
                        exc_info=error,
                        guild_id=guild.id,
                        **exception_fields(error),
                    )

    @discord.slash_command(name="solicitar-ficha", description="Programa una ficha del FTP")
    async def request_training(self, ctx: discord.ApplicationContext) -> None:
        if ctx.guild is None or not isinstance(ctx.author, discord.Member):
            await ctx.respond(
                embed=discord.Embed(
                    title="⚠️ Solicitud FTP no disponible",
                    description="Este comando solo funciona dentro de un servidor.",
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )
            return
        try:
            service = await self.registry.get(ctx.guild.id, require_enabled=False)
        except LookupError:
            service = None
        if service is None or not service.enabled or service.request_options is None:
            await ctx.respond(embed=_unavailable_embed(service), ephemeral=True)
            return
        if not service.can_request(ctx.author):
            await ctx.respond(
                embed=service.alert(service.ui.unauthorized_requester),
                ephemeral=True,
            )
            return
        if ctx.channel is None or not service.is_command_channel(ctx.channel.id):
            await ctx.respond(
                embed=service.alert(
                    service.ui.wrong_channel.format(channel=service.command_channel_mentions())
                ),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        try:
            await service.destination_channel(ctx.guild)
        except Exception as error:
            service._log_error("ftp_request_destination_unavailable", error, ctx.interaction)
            await ctx.respond(embed=service.alert(service.ui.unavailable), ephemeral=True)
            return
        await ctx.respond(
            "Selecciona la zona horaria que usarás para esta solicitud.",
            view=FtpTimezoneView(service, ctx.author),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.slash_command(name="info-ftp", description="Explica cómo solicitar una ficha FTP")
    async def info_ftp(self, ctx: discord.ApplicationContext) -> None:
        if ctx.guild is None:
            await ctx.respond(
                embed=discord.Embed(
                    title="⚠️ Información FTP no disponible",
                    description="Este comando solo funciona dentro de un servidor.",
                    color=discord.Color.red(),
                ),
                ephemeral=True,
            )
            return
        try:
            service = await self.registry.get(ctx.guild.id, require_enabled=False)
        except LookupError:
            service = None
        if service is None or not service.enabled or service.request_options is None:
            await ctx.respond(embed=_unavailable_embed(service), ephemeral=True)
            return
        await ctx.respond(
            embed=build_ftp_info_embed(service),
            ephemeral=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.slash_command(name="fichas-activas", description="Muestra las fichas FTP pendientes")
    async def active_requests(self, ctx: discord.ApplicationContext) -> None:
        if ctx.guild is None or not isinstance(ctx.author, discord.Member):
            await ctx.respond(
                embed=_unavailable_embed(None),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        try:
            service = await self.registry.get(ctx.guild.id, require_enabled=False)
        except LookupError:
            service = None
        if service is None or not service.enabled or service.request_options is None:
            await ctx.respond(
                embed=_unavailable_embed(service),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if not service.can_list_active(ctx.author):
            await ctx.respond(
                embed=service.alert(
                    "Este comando está reservado al personal operativo autorizado."
                ),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        view = FtpActiveRequestsView(service, ctx.author.id)
        try:
            embed = await view.render(ctx.guild.id)
        except Exception as error:
            service._log_error("ftp_active_requests_failed", error, ctx.interaction)
            await ctx.respond(
                embed=service.alert(service.ui.operation_failed),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await ctx.respond(
            embed=embed,
            view=view,
            ephemeral=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )


def build_ftp_request_embed(
    request: FtpTrainingRequest,
    *,
    requester: discord.Member | None = None,
) -> discord.Embed:
    status, color = {
        FtpRequestStatus.PUBLISHING: ("Preparando publicación", discord.Color.gold()),
        FtpRequestStatus.PENDING: ("Esperando instructor", discord.Color.gold()),
        FtpRequestStatus.CLAIMED: ("Atendida", discord.Color.green()),
        FtpRequestStatus.CANCELLED: ("Cancelada", discord.Color.red()),
        FtpRequestStatus.FAILED: ("No publicada", discord.Color.red()),
    }[request.status]
    embed = discord.Embed(
        title="🎓 Solicitud de ficha FTP",
        color=color,
        timestamp=request.created_at,
    )
    if requester is not None:
        embed.set_author(
            name=requester.display_name,
            icon_url=str(requester.display_avatar.url),
        )
    else:
        embed.set_author(name="Field Training Trainee")
    embed.add_field(name="Trainee", value=f"<@{request.requester_user_id}>", inline=True)
    if request.training_phase is not None:
        embed.add_field(name="Ficha", value=request.training_phase.label, inline=True)
    if request.requested_instructor_level is not None:
        embed.add_field(
            name="Tipo de evaluador",
            value=request.requested_instructor_level.label,
            inline=True,
        )
    if request.scheduled_for is not None:
        timestamp = int(request.scheduled_for.timestamp())
        embed.add_field(
            name="Fecha",
            value=f"<t:{timestamp}:F>\n<t:{timestamp}:R>",
            inline=False,
        )
    if comments := _safe_user_text(request.reason):
        embed.add_field(name="Comentarios", value=comments, inline=False)
    embed.add_field(name="Estado", value=status, inline=True)
    if request.auto_cancelled_at is not None:
        embed.color = discord.Color.dark_grey()
        # Keep automatic cancellation distinct from a decision made by a member.
        embed.set_field_at(
            len(embed.fields) - 1, name="Estado", value="⌛ Cancelada automáticamente", inline=True,
        )
        embed.add_field(
            name="Motivo",
            value=(
                ("El horario solicitado pasó y nadie tomó la solicitud dentro del plazo. "
                 if request.scheduled_for is not None
                 else "La solicitud quedó sin tomar durante el plazo disponible. ")
                + "Puedes crear otra si aún necesitas la ficha."
            ),
            inline=False,
        )
    elif request.status is FtpRequestStatus.CLAIMED and request.claimed_by_user_id is not None:
        embed.add_field(
            name="Instructor asignado",
            value=f"<@{request.claimed_by_user_id}>",
            inline=False,
        )
    elif request.status is FtpRequestStatus.CANCELLED and request.resolved_by_user_id is not None:
        embed.add_field(
            name="Cancelada por",
            value=f"<@{request.resolved_by_user_id}>",
            inline=False,
        )
    embed.set_footer(text="Field Training Program")
    return embed


def build_ftp_info_embed(service: FtpRequestService) -> discord.Embed:
    options = service.request_options
    if options is None:
        raise LookupError("FTP request destination is not configured")
    embed = discord.Embed(
        title="🎓 Fichas del Field Training Program",
        description=service.ui.info_intro,
        color=discord.Color.gold(),
    )
    alternate_channels = options.additional_command_channel_ids
    command_alternative = ""
    if alternate_channels:
        mentions = " o ".join(f"<#{channel_id}>" for channel_id in alternate_channels)
        command_alternative = f" También puedes utilizar `/solicitar-ficha` desde {mentions}."
    embed.add_field(
        name="1️⃣ Solicitar una ficha",
        value=(
            f"Ve a <#{options.channel_id}> y pulsa **Solicitar ficha** en el mensaje fijado. "
            "Selecciona tu zona horaria y luego indica la fase, la fecha, la hora y si necesitas "
            "una evaluación general o de supervisor. "
            f"También puedes dejar indicaciones adicionales si hacen falta.{command_alternative}"
        ),
        inline=False,
    )
    embed.add_field(
        name="2️⃣ Publicación",
        value=(
            "Roy publicará la solicitud y avisará al grupo correspondiente. "
            "La fecha se mostrará en la hora local de cada persona."
        ),
        inline=False,
    )
    embed.add_field(
        name="3️⃣ Cuando la tomen",
        value=(
            "La tarjeta mostrará al instructor asignado y recibirás una mención para que puedan coordinarse."
        ),
        inline=False,
    )
    embed.add_field(
        name="4️⃣ Cancelación",
        value=(
            "Mientras siga pendiente, puedes cancelarla desde la propia solicitud."
            + (f" Si nadie la toma, se cancelará automáticamente después de "
               f"{options.expiry_grace_hours} horas desde el horario solicitado."
               if options.expiry_enabled else "")
        ),
        inline=False,
    )
    embed.add_field(
        name="5️⃣ Solicitudes activas",
        value="El personal operativo puede usar `/fichas-activas` desde cualquier canal.",
        inline=False,
    )
    embed.add_field(
        name="👨‍🏫 Ofrecer disponibilidad",
        value=(
            "Los FTO, MFTO, Supervisory, Command y Executive pueden usar `/ofrecer-ficha` "
            f"en {service.command_channel_mentions()} o el botón **Ofrecer disponibilidad** "
            "del mensaje fijado. "
            "Elige **Disponible ahora** o una fecha y hora en tu zona horaria, la duración y de 1 a 5 "
            "bloques seguidos. Para horarios separados, publica otra disponibilidad."
        ), inline=False,
    )
    embed.add_field(
        name="🗓️ Reservar un bloque",
        value=(
            "Solo los Field Training Trainees pueden reservar: selecciona un bloque disponible, "
            "indica la fase y confirma el formulario. La tarjeta mostrará tu reserva y se avisará a ambos. "
            "Los bloques libres vencen al terminar su horario. "
            "Cerrar una disponibilidad no cancela las reservas."
        ), inline=False,
    )
    embed.add_field(
        name="Quién puede solicitar",
        value="El botón **Solicitar ficha** está disponible para FTT y, por soporte, Command o Executive.",
        inline=False,
    )
    embed.set_footer(text="Field Training Program")
    return embed


def build_active_ftp_requests_embed(
    entries: tuple[FtpTrainingRequest, ...],
    total: int,
    page: int,
) -> discord.Embed:
    embed = discord.Embed(
        title="🎓 Solicitudes de fichas activas",
        description=f"**{total} solicitud{'es' if total != 1 else ''} pendiente{'s' if total != 1 else ''}**",
        color=discord.Color.gold(),
    )
    for request in entries:
        phase = request.training_phase.label if request.training_phase else "Ficha FTP"
        level = request.requested_instructor_level
        level_label = level.label if level else "Por coordinar"
        level_emoji = "⭐" if level is FtpInstructorLevel.SUPERVISOR else "👨‍🏫"
        scheduled = (
            discord.utils.format_dt(request.scheduled_for, style="f")
            if request.scheduled_for is not None
            else "Horario sin definir"
        )
        link = f"[Ver solicitud]({request.jump_url})" if request.jump_url else "Sin enlace"
        embed.add_field(
            name=f"{phase} · {level_emoji} {level_label}",
            value=f"<@{request.requester_user_id}> · {scheduled} · {link}",
            inline=False,
        )
    if total == 0:
        embed.description = "No hay solicitudes pendientes en este momento."
    pages = max(1, math.ceil(total / ACTIVE_REQUEST_PAGE_SIZE))
    embed.set_footer(text=f"Página {page + 1}/{pages} · {total} activa(s)")
    return embed


def build_ftp_request_panel_embed(service: FtpRequestService) -> discord.Embed:
    embed = discord.Embed(
        title="🎓 Fichas FTP",
        description=(
            f"{service.ui.info_intro}\n\n"
            "Pulsa **Solicitar ficha**, selecciona tu zona horaria e indica la fase, fecha, hora, tipo de "
            "evaluador y cualquier "
            "comentario adicional. Puedes mantener varias solicitudes para horarios distintos."
            "\n\n**Instructores:** usa **Ofrecer disponibilidad** para publicar tus bloques libres. "
            "Los trainees podrán reservarlos desde la tarjeta eligiendo la fase que necesitan."
        ),
        color=discord.Color.gold(),
    )
    embed.set_footer(text="Field Training Program")
    return embed


def parse_ftp_schedule(
    raw_date: str,
    raw_time: str,
    timezone_key: str,
) -> datetime:
    if timezone_key not in FTP_TIMEZONE_KEYS:
        raise ValueError("Invalid FTP request timezone")
    try:
        local = datetime.strptime(
            f"{raw_date.strip()} {raw_time.strip()}",
            "%d/%m/%Y %H:%M",
        ).replace(tzinfo=ZoneInfo(timezone_key))
    except ValueError as error:
        raise ValueError("Invalid FTP request schedule") from error
    return local.astimezone(UTC)


def _has_any_role(member: discord.Member, role_ids: tuple[int, ...]) -> bool:
    allowed = set(role_ids)
    return bool(allowed and any(role.id in allowed for role in member.roles))


def _safe_user_text(value: str) -> str:
    return discord.utils.escape_mentions(discord.utils.escape_markdown(value.strip()))


def _unavailable_embed(service: FtpRequestService | None) -> discord.Embed:
    if service is not None:
        return service.alert(service.ui.unavailable)
    return discord.Embed(
        title="⚠️ Solicitud FTP no disponible",
        description="Las solicitudes de instrucción FTP no están habilitadas para este servidor.",
        color=discord.Color.red(),
    )

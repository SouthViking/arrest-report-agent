"""Discord UI for deterministic FTP assistance requests."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands

from arrest_report_agent.config import BotPersona, FtpRequestOptions, GuildRuntimeSettings
from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.infrastructure.postgres import DatabaseConfigProvider

from .models import (
    FtpInstructorLevel,
    FtpRequestStatus,
    FtpTrainingPhase,
    FtpTrainingRequest,
)
from .repository import FtpRequestRepository

LOGGER = logging.getLogger(__name__)
MODAL_TIMEOUT_SECONDS = 300
FTP_TIMEZONE = ZoneInfo("America/Santiago")
MAX_SCHEDULE_DAYS = 180


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
        now = datetime.now(UTC)
        if (
            scheduled_for.tzinfo is None
            or scheduled_for <= now
            or scheduled_for > now + timedelta(days=MAX_SCHEDULE_DAYS)
        ):
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
        phase = request.training_phase.label if request.training_phase else "la ficha solicitada"
        requester = discord.Object(id=request.requester_user_id)
        try:
            await interaction.message.reply(
                content=(
                    f"<@{request.requester_user_id}>, <@{instructor.id}> tomó tu solicitud "
                    f"para **{phase}**. Ya pueden coordinar los detalles. 🚓"
                ),
                allowed_mentions=discord.AllowedMentions(
                    everyone=False,
                    users=[requester],
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
        await interaction.response.send_modal(FtpRequestModal(service, member))


class FtpRequestModal(discord.ui.DesignerModal):
    def __init__(self, service: FtpRequestService, member: discord.Member) -> None:
        self.service = service
        self.member_id = member.id
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
            discord.ui.Label("Hora", self.time),
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
        if ctx.channel is None or ctx.channel.id != service.request_options.channel_id:
            channel_mention = f"<#{service.request_options.channel_id}>"
            await ctx.respond(
                embed=service.alert(service.ui.wrong_channel.format(channel=channel_mention)),
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
        await ctx.send_modal(FtpRequestModal(service, ctx.author))

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
    if request.status is FtpRequestStatus.CLAIMED and request.claimed_by_user_id is not None:
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
    embed.add_field(
        name="1️⃣ Solicitar una ficha",
        value=(
            f"Ve a <#{options.channel_id}> y pulsa **Solicitar ficha** en el mensaje fijado. "
            "Selecciona la fase, la fecha, la hora y si necesitas una evaluación general o de supervisor. "
            "También puedes dejar indicaciones adicionales si hacen falta."
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
        value="Mientras siga pendiente, puedes cancelarla desde la propia solicitud.",
        inline=False,
    )
    embed.add_field(name="Importante", value=service.ui.info_closing, inline=False)
    embed.set_footer(text="Field Training Program")
    return embed


def build_ftp_request_panel_embed(service: FtpRequestService) -> discord.Embed:
    embed = discord.Embed(
        title="🎓 Solicitudes de fichas FTP",
        description=(
            f"{service.ui.info_intro}\n\n"
            "Pulsa el botón para indicar la fase, fecha, hora, tipo de evaluador y cualquier "
            "comentario adicional. Puedes mantener varias solicitudes para horarios distintos."
        ),
        color=discord.Color.gold(),
    )
    embed.set_footer(text="Field Training Program")
    return embed


def parse_ftp_schedule(
    raw_date: str,
    raw_time: str,
    *,
    now: datetime | None = None,
) -> datetime:
    try:
        local = datetime.strptime(
            f"{raw_date.strip()} {raw_time.strip()}",
            "%d/%m/%Y %H:%M",
        ).replace(tzinfo=FTP_TIMEZONE)
    except ValueError as error:
        raise ValueError("Invalid FTP request schedule") from error
    scheduled_for = local.astimezone(UTC)
    reference = now or datetime.now(UTC)
    if reference.tzinfo is None:
        raise ValueError("Reference time must include timezone information")
    reference = reference.astimezone(UTC)
    if scheduled_for <= reference:
        raise ValueError("FTP request schedule must be in the future")
    if scheduled_for > reference + timedelta(days=MAX_SCHEDULE_DAYS):
        raise ValueError("FTP request schedule is too far in the future")
    return scheduled_for


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

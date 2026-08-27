"""Discord UI for deterministic FTP assistance requests."""

from __future__ import annotations

import logging
from contextlib import suppress
from typing import cast
from uuid import UUID

import discord
from discord.ext import commands

from arrest_report_agent.config import BotPersona, FtpRequestOptions, GuildRuntimeSettings
from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.infrastructure.postgres import DatabaseConfigProvider

from .models import FtpRequestStatus, FtpTrainingRequest
from .repository import ActiveFtpRequestError, FtpRequestRepository

LOGGER = logging.getLogger(__name__)
MODAL_TIMEOUT_SECONDS = 300


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
        return _has_any_role(member, (*ftp.all_role_ids, *options.management_role_ids))

    def can_claim(self, member: discord.Member) -> bool:
        ftp = self.settings.ftp_status_options
        options = self.request_options
        if ftp is None or options is None:
            return False
        return _has_any_role(
            member,
            (
                *ftp.supervisory_role_ids,
                *ftp.mfto_role_ids,
                *ftp.fto_role_ids,
                *options.management_role_ids,
            ),
        )

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

    def notification_roles(self, guild: discord.Guild) -> tuple[discord.Role, ...]:
        ftp = self.settings.ftp_status_options
        if ftp is None:
            return ()
        role_ids = (*ftp.supervisory_role_ids, *ftp.mfto_role_ids, *ftp.fto_role_ids)
        return tuple(role for role_id in role_ids if (role := guild.get_role(role_id)) is not None)

    async def create(
        self,
        interaction: discord.Interaction,
        *,
        member: discord.Member,
        reason: str,
    ) -> None:
        normalized_reason = reason.strip()
        if not 10 <= len(normalized_reason) <= 500:
            await interaction.response.send_message(
                embed=self.alert(self.ui.invalid_reason),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await interaction.response.defer(ephemeral=True)
        request: FtpTrainingRequest | None = None
        message: discord.Message | None = None
        try:
            channel = await self.destination_channel(member.guild)
            roles = self.notification_roles(member.guild)
            if not roles:
                raise LookupError("No FTP notification roles are available")
            request = await self.repository.create_publishing(
                guild_id=member.guild.id,
                channel_id=channel.id,
                requester_user_id=member.id,
                reason=normalized_reason,
            )
            message = await channel.send(
                content=" ".join(role.mention for role in roles),
                embed=build_ftp_request_embed(request),
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
                embed=build_ftp_request_embed(request),
                view=FtpRequestControlsView(),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            link = f"\n\n[Ver solicitud]({request.jump_url})" if request.jump_url else ""
            await interaction.followup.send(
                embed=discord.Embed(
                    title="🎓 Solicitud FTP publicada",
                    description=f"{self.ui.published}{link}",
                    color=discord.Color.green(),
                ),
                ephemeral=True,
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
        except ActiveFtpRequestError:
            await interaction.followup.send(
                embed=self.alert(self.ui.active_request),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
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
        if not isinstance(member, discord.Member) or not self.can_claim(member):
            await interaction.response.send_message(
                embed=self.alert(self.ui.unauthorized_acceptor), ephemeral=True
            )
            return
        if member.id == request.requester_user_id:
            await interaction.response.send_message(
                embed=self.alert(self.ui.requester_cannot_accept), ephemeral=True
            )
            return
        if request.status is not FtpRequestStatus.PENDING:
            await interaction.response.send_message(embed=self.alert(self.ui.stale), ephemeral=True)
            return
        await interaction.response.defer()
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
        await interaction.response.defer()
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

    @staticmethod
    async def _edit_interaction_message(
        interaction: discord.Interaction,
        request: FtpTrainingRequest,
    ) -> None:
        if interaction.message is None:
            raise LookupError("FTP request Discord message is unavailable")
        await interaction.message.edit(
            content=None,
            embed=build_ftp_request_embed(request),
            view=FtpRequestControlsView(disabled=True),
            allowed_mentions=discord.AllowedMentions.none(),
        )

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
        label="Tomar solicitud",
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
            await interaction.response.send_message(
                embed=_unavailable_embed(service), ephemeral=True
            )
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
            await interaction.response.send_message(
                embed=_unavailable_embed(service), ephemeral=True
            )
            return
        await service.cancel(interaction)


class FtpRequestModal(discord.ui.DesignerModal):
    def __init__(self, service: FtpRequestService, member: discord.Member) -> None:
        self.service = service
        self.member_id = member.id
        self.reason = discord.ui.InputText(
            custom_id="ftp-request:v1:create:reason",
            style=discord.InputTextStyle.long,
            placeholder="Explique brevemente qué entrenamiento o apoyo necesita.",
            min_length=10,
            max_length=500,
        )
        super().__init__(
            discord.ui.Label("Motivo de la solicitud", self.reason),
            title="Solicitar un instructor FTP",
            custom_id=f"ftp-request:v1:create:{member.id}",
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
        await self.service.create(interaction, member=member, reason=self.reason.value or "")


class FtpRequestCog(commands.Cog):
    def __init__(self, registry: FtpRequestServiceRegistry, persona: BotPersona) -> None:
        self.registry = registry
        self.request_fto.description = persona.messages.interface.commands.ftp_request_description

    @discord.slash_command(name="solicitar-fto", description="Solicita un instructor del FTP")
    async def request_fto(self, ctx: discord.ApplicationContext) -> None:
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
        try:
            await service.destination_channel(ctx.guild)
        except Exception as error:
            service._log_error("ftp_request_destination_unavailable", error, ctx.interaction)
            await ctx.respond(embed=service.alert(service.ui.unavailable), ephemeral=True)
            return
        await ctx.send_modal(FtpRequestModal(service, ctx.author))


def build_ftp_request_embed(request: FtpTrainingRequest) -> discord.Embed:
    status, color = {
        FtpRequestStatus.PUBLISHING: ("Preparando publicación", discord.Color.gold()),
        FtpRequestStatus.PENDING: ("Esperando instructor", discord.Color.gold()),
        FtpRequestStatus.CLAIMED: ("Atendida", discord.Color.green()),
        FtpRequestStatus.CANCELLED: ("Cancelada", discord.Color.red()),
        FtpRequestStatus.FAILED: ("No publicada", discord.Color.red()),
    }[request.status]
    embed = discord.Embed(
        title="🎓 Solicitud de instrucción FTP",
        description=_safe_user_text(request.reason),
        color=color,
        timestamp=request.created_at,
    )
    embed.add_field(name="Solicitante", value=f"<@{request.requester_user_id}>", inline=True)
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

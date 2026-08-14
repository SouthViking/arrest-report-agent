"""Discord UI for the isolated deterministic pager subsystem."""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Iterable
from contextlib import asynccontextmanager, suppress
from uuid import UUID

import discord
from discord.ext import commands

from arrest_report_agent.config import (
    AccessPolicyKey,
    BotPersona,
    GuildRuntimeSettings,
)
from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.infrastructure.postgres import DatabaseConfigProvider
from arrest_report_agent.operational_profiles import MemberProfileRepository

from .models import Pager, PagerCreatorCannotRespondError, PagerStatus, UserPager
from .repository import PagerRepository

LOGGER = logging.getLogger(__name__)
PAGE_SIZE = 5
MODAL_TIMEOUT_SECONDS = 300
LIST_TIMEOUT_SECONDS = 900


class PagerValidationError(ValueError):
    def __init__(self, public_message: str) -> None:
        super().__init__(public_message)
        self.public_message = public_message


class PagerServiceRegistry:
    """Resolve one immutable service per published guild configuration."""

    def __init__(
        self,
        bot: discord.Bot,
        provider: DatabaseConfigProvider,
        repository: PagerRepository,
        member_profiles: MemberProfileRepository,
    ) -> None:
        self.bot = bot
        self.provider = provider
        self.repository = repository
        self.member_profiles = member_profiles
        self._services: dict[UUID, PagerService] = {}

    async def get(self, guild_id: int, *, require_enabled: bool = True) -> PagerService:
        bundle = await self.provider.cached(guild_id)
        feature = bundle.snapshot.features.get("pagers")
        enabled = bool(feature and feature.enabled)
        if require_enabled and not enabled:
            raise LookupError(f"Pagers are disabled for guild {guild_id}")
        key = bundle.snapshot.revision_id
        service = self._services.get(key)
        if service is None:
            service = PagerService(
                self.bot,
                self.repository,
                bundle.settings,
                bundle.persona.profile,
                enabled=enabled,
                member_profiles=self.member_profiles,
            )
            self._services[key] = service
        return service


class PagerService:
    def __init__(
        self,
        bot: discord.Bot,
        repository: PagerRepository,
        settings: GuildRuntimeSettings,
        persona: BotPersona,
        *,
        enabled: bool,
        member_profiles: MemberProfileRepository | None = None,
    ) -> None:
        self.bot = bot
        self.repository = repository
        self.settings = settings
        self.persona = persona
        self.ui = persona.messages.interface.pagers
        self.callsign_ui = persona.messages.interface.callsigns
        self.enabled = enabled
        self.member_profiles = member_profiles
        self._locks: dict[UUID, asyncio.Lock] = {}
        self._lock_users: dict[UUID, int] = {}

    def alert(self, message: str) -> discord.Embed:
        return discord.Embed(title=self.ui.alert_title, description=message, color=discord.Color.red())

    def channel_allowed(self, channel: discord.abc.GuildChannel) -> bool:
        return isinstance(channel, discord.TextChannel) and self.settings.pager_channel_allowlist.allows(
            channel.id
        )

    def can_create(self, member: object) -> bool:
        return _has_any_role(member, self.settings.pager_creator_role_ids)

    def can_close(self, member: discord.Member, pager: Pager) -> bool:
        return member.id == pager.creator_user_id or _has_any_role(
            member, self.settings.pager_closer_role_ids
        )

    def can_edit(self, member: discord.Member, pager: Pager) -> bool:
        return self.can_close(member, pager)

    def can_respond(self, member: object) -> bool:
        return _has_any_role(member, self.settings.operational_staff_role_ids)

    def can_view_info(self, member: object) -> bool:
        """Allow the guide only to members who can participate in pager operations."""

        return (
            self.can_create(member)
            or self.can_respond(member)
            or _has_any_role(member, self.settings.pager_closer_role_ids)
        )

    async def saved_callsign(self, member: discord.Member) -> str | None:
        if self.member_profiles is None:
            return None
        return await self.member_profiles.get_callsign(member.guild.id, member.id)

    def recipient_roles(self, guild: discord.Guild) -> tuple[discord.Role, ...]:
        result = []
        for role_id in self.settings.pager_recipient_role_ids:
            role = guild.get_role(role_id)
            if role is not None:
                result.append(role)
        return tuple(result)

    @staticmethod
    def can_notify_role(channel: discord.TextChannel, role: discord.Role) -> bool:
        if role.mentionable:
            return True
        bot_member = channel.guild.me
        return bool(bot_member is not None and channel.permissions_for(bot_member).mention_everyone)

    def validate_recipients(
        self,
        channel: discord.TextChannel,
        role_ids: tuple[int, ...],
    ) -> tuple[discord.Role, ...]:
        if not role_ids or len(role_ids) > 25 or len(role_ids) != len(set(role_ids)):
            raise PagerValidationError(self.ui.invalid_recipient)
        roles: list[discord.Role] = []
        for role_id in role_ids:
            if role_id not in self.settings.pager_recipient_role_ids:
                raise PagerValidationError(self.ui.invalid_recipient)
            role = channel.guild.get_role(role_id)
            if role is None or not self.can_notify_role(channel, role):
                raise PagerValidationError(self.ui.invalid_recipient)
            roles.append(role)
        return tuple(roles)

    async def allowed_channel_summary(self, guild: discord.Guild) -> str:
        channel_ids = self.settings.snapshot.policy(AccessPolicyKey.PAGER_CHANNELS).ids
        channels = [
            channel
            for channel_id in channel_ids
            if isinstance((channel := guild.get_channel(channel_id)), discord.TextChannel)
        ]
        return "\n".join(f"• {channel.mention}" for channel in channels) or "• Sin canales disponibles"

    async def create(
        self,
        interaction: discord.Interaction,
        *,
        channel: discord.TextChannel,
        member: discord.Member,
        callsign: str,
        recipient_role_ids: tuple[int, ...],
        location: str,
        situation: str,
    ) -> None:
        if not self.enabled or not self.can_create(member) or not self.channel_allowed(channel):
            await interaction.response.send_message(
                embed=self.alert(self.ui.unavailable),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if not all(value.strip() for value in (callsign, location, situation)):
            await interaction.response.send_message(
                embed=self.alert("Completa todos los datos obligatorios antes de emitir el pager."),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        try:
            roles = self.validate_recipients(channel, recipient_role_ids)
        except PagerValidationError as error:
            await interaction.response.send_message(
                embed=self.alert(error.public_message),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        await interaction.response.defer()
        pager: Pager | None = None
        message: discord.Message | None = None
        try:
            pager = await self.repository.create_publishing(
                guild_id=member.guild.id,
                channel_id=channel.id,
                creator_user_id=member.id,
                creator_callsign=callsign.strip(),
                recipient_role_ids=tuple(role.id for role in roles),
                location=location.strip(),
                situation=situation.strip(),
                starting_number=self.settings.pager_starting_number,
            )
            message = await channel.send(
                content=_recipient_header(pager),
                embed=self.build_embed(pager),
                view=PagerControlsView(),
                allowed_mentions=discord.AllowedMentions(
                    everyone=False,
                    users=False,
                    roles=list(roles),
                    replied_user=False,
                ),
            )
            pager = await self.repository.publish(pager.id, message.id)
            await message.edit(
                embed=self.build_embed(pager),
                view=PagerControlsView(),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            log_event(
                LOGGER,
                "info",
                "pager_created",
                guild_id=pager.guild_id,
                channel_id=pager.channel_id,
                message_id=message.id,
                pager_id=str(pager.id),
                pager_number=pager.number,
                actor_user_id=member.id,
                transition="publishing->active",
            )
        except Exception as error:
            if pager is not None:
                with suppress(Exception):
                    await self.repository.mark_failed(pager.id, resolution="Discord publication failed")
            if message is not None:
                with suppress(discord.Forbidden, discord.HTTPException):
                    await message.delete()
            log_event(
                LOGGER,
                "error",
                "pager_creation_failed",
                exc_info=error,
                guild_id=member.guild.id,
                channel_id=channel.id,
                actor_user_id=member.id,
                **exception_fields(error),
            )
            await interaction.followup.send(
                embed=self.alert(self.ui.operation_failed),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )

    async def resolve_interaction_pager(self, interaction: discord.Interaction) -> Pager:
        if interaction.guild is None or interaction.message is None:
            raise PagerValidationError(self.ui.stale)
        pager = await self.repository.get_by_message(
            interaction.guild.id,
            interaction.channel_id,
            interaction.message.id,
        )
        if pager is None:
            raise PagerValidationError(self.ui.stale)
        return pager

    async def respond(
        self,
        interaction: discord.Interaction,
        pager_id: UUID,
        callsign: str,
        *,
        remember: bool = False,
    ) -> None:
        if not self.can_respond(interaction.user):
            await interaction.response.send_message(
                embed=self.alert(self.callsign_ui.unauthorized),
                ephemeral=True,
            )
            return
        resolved_callsign = callsign.strip() if remember else callsign
        if remember and not resolved_callsign:
            await interaction.response.send_message(
                embed=self.alert("Indica un callsign válido."),
                ephemeral=True,
            )
            return
        try:
            current = await self.repository.get(pager_id)
        except (LookupError, ValueError) as error:
            await interaction.response.send_message(embed=self.alert(self.ui.stale), ephemeral=True)
            self._log_error("pager_response_failed", error, pager_id, interaction)
            return
        if current.creator_user_id == interaction.user.id:
            await interaction.response.send_message(
                embed=self.alert(self.ui.creator_cannot_respond), ephemeral=True
            )
            return
        if current.status is not PagerStatus.ACTIVE:
            await interaction.response.send_message(embed=self.alert(self.ui.stale), ephemeral=True)
            return
        await interaction.response.defer()
        try:
            async with self.lock(pager_id):
                if remember:
                    if self.member_profiles is None or interaction.guild_id is None:
                        raise RuntimeError("Operational member profile repository is unavailable")
                    await self.member_profiles.upsert_callsign(
                        interaction.guild_id,
                        interaction.user.id,
                        resolved_callsign,
                    )
                pager = await self.repository.upsert_responder(
                    pager_id,
                    interaction.user.id,
                    resolved_callsign,
                )
                await self.refresh_message(pager)
            self._log_transition("pager_responder_upserted", pager, interaction.user.id, "responded")
        except PagerCreatorCannotRespondError as error:
            await interaction.followup.send(embed=self.alert(self.ui.creator_cannot_respond), ephemeral=True)
            self._log_error("pager_response_rejected", error, pager_id, interaction)
        except (LookupError, ValueError) as error:
            await interaction.followup.send(embed=self.alert(self.ui.stale), ephemeral=True)
            self._log_error("pager_response_failed", error, pager_id, interaction)
        except Exception as error:
            await interaction.followup.send(embed=self.alert(self.ui.operation_failed), ephemeral=True)
            self._log_error("pager_response_failed", error, pager_id, interaction)

    async def withdraw(self, interaction: discord.Interaction) -> None:
        try:
            pager = await self.resolve_interaction_pager(interaction)
        except PagerValidationError as error:
            await interaction.response.send_message(embed=self.alert(error.public_message), ephemeral=True)
            return
        await interaction.response.defer()
        try:
            async with self.lock(pager.id):
                pager, removed = await self.repository.remove_responder(pager.id, interaction.user.id)
                if removed:
                    await self.refresh_message(pager)
            if not removed:
                await interaction.followup.send(
                    embed=self.alert("No figurabas como respondiente en este pager."),
                    ephemeral=True,
                )
                return
            self._log_transition("pager_responder_removed", pager, interaction.user.id, "withdrawn")
        except (LookupError, ValueError) as error:
            await interaction.followup.send(embed=self.alert(self.ui.stale), ephemeral=True)
            self._log_error("pager_withdraw_failed", error, pager.id, interaction)
        except Exception as error:
            await interaction.followup.send(embed=self.alert(self.ui.operation_failed), ephemeral=True)
            self._log_error("pager_withdraw_failed", error, pager.id, interaction)

    async def edit(
        self,
        interaction: discord.Interaction,
        pager_id: UUID,
        *,
        channel: discord.TextChannel,
        callsign: str,
        recipient_role_ids: tuple[int, ...],
        location: str,
        situation: str,
    ) -> None:
        member = interaction.user
        try:
            current = await self.repository.get(pager_id)
        except (LookupError, ValueError) as error:
            await interaction.response.send_message(embed=self.alert(self.ui.stale), ephemeral=True)
            self._log_error("pager_edit_failed", error, pager_id, interaction)
            return
        if not isinstance(member, discord.Member) or not self.can_edit(member, current):
            await interaction.response.send_message(
                embed=self.alert(self.ui.unauthorized_editor), ephemeral=True
            )
            return
        if current.status is not PagerStatus.ACTIVE:
            await interaction.response.send_message(embed=self.alert(self.ui.stale), ephemeral=True)
            return
        if not all(value.strip() for value in (callsign, location, situation)):
            await interaction.response.send_message(
                embed=self.alert("Completa todos los datos obligatorios antes de guardar."),
                ephemeral=True,
            )
            return
        try:
            roles = self.validate_recipients(channel, recipient_role_ids)
        except PagerValidationError as error:
            await interaction.response.send_message(embed=self.alert(error.public_message), ephemeral=True)
            return

        await interaction.response.defer()
        previous: Pager | None = None
        updated = False
        try:
            async with self.lock(pager_id):
                previous = await self.repository.get(pager_id)
                if not self.can_edit(member, previous):
                    await interaction.followup.send(
                        embed=self.alert(self.ui.unauthorized_editor), ephemeral=True
                    )
                    return
                if previous.status is not PagerStatus.ACTIVE:
                    await interaction.followup.send(embed=self.alert(self.ui.stale), ephemeral=True)
                    return
                pager = await self.repository.update_active(
                    pager_id,
                    creator_callsign=callsign.strip(),
                    recipient_role_ids=tuple(role.id for role in roles),
                    location=location.strip(),
                    situation=situation.strip(),
                )
                updated = True
                await self.refresh_message(pager)
            self._log_transition("pager_edited", pager, member.id, "active->active")
        except (LookupError, ValueError) as error:
            await interaction.followup.send(embed=self.alert(self.ui.stale), ephemeral=True)
            self._log_error("pager_edit_failed", error, pager_id, interaction)
        except Exception as error:
            if updated and previous is not None:
                with suppress(Exception):
                    await self.repository.update_active(
                        pager_id,
                        creator_callsign=previous.creator_callsign,
                        recipient_role_ids=previous.recipient_role_ids,
                        location=previous.location,
                        situation=previous.situation,
                    )
            await interaction.followup.send(embed=self.alert(self.ui.operation_failed), ephemeral=True)
            self._log_error("pager_edit_failed", error, pager_id, interaction)

    async def close(
        self,
        interaction: discord.Interaction,
        pager_id: UUID,
        *,
        status: PagerStatus,
        resolution: str,
    ) -> None:
        if not resolution.strip():
            await interaction.response.send_message(
                embed=self.alert("Indica el resultado o motivo antes de cerrar el pager."),
                ephemeral=True,
            )
            return
        await interaction.response.defer()
        member = interaction.user
        changed = False
        try:
            async with self.lock(pager_id):
                pager = await self.repository.get(pager_id)
                if not isinstance(member, discord.Member) or not self.can_close(member, pager):
                    await interaction.followup.send(
                        embed=self.alert(self.ui.unauthorized_closer), ephemeral=True
                    )
                    return
                pager, changed = await self.repository.close(
                    pager_id,
                    status=status,
                    resolution=resolution.strip(),
                    actor_user_id=member.id,
                )
                await self.refresh_message(pager)
            if not changed:
                await interaction.followup.send(embed=self.alert(self.ui.stale), ephemeral=True)
                return
            self._log_transition(
                "pager_closed",
                pager,
                member.id,
                f"active->{status.value}",
            )
        except (LookupError, ValueError) as error:
            await interaction.followup.send(embed=self.alert(self.ui.stale), ephemeral=True)
            self._log_error("pager_close_failed", error, pager_id, interaction)
        except Exception as error:
            if changed:
                with suppress(Exception):
                    await self.repository.restore_active_after_delivery_failure(
                        pager_id,
                        expected_status=status,
                    )
            await interaction.followup.send(embed=self.alert(self.ui.operation_failed), ephemeral=True)
            self._log_error("pager_close_failed", error, pager_id, interaction)

    async def refresh_message(self, pager: Pager) -> None:
        if pager.message_id is None:
            raise LookupError("Pager has no Discord message")
        channel = self.bot.get_channel(pager.channel_id)
        if channel is None:
            channel = await self.bot.fetch_channel(pager.channel_id)
        if not isinstance(channel, discord.TextChannel):
            raise LookupError("Pager channel is unavailable")
        message = await channel.fetch_message(pager.message_id)
        await message.edit(
            content=_recipient_header(pager),
            embed=self.build_embed(pager),
            view=PagerControlsView(disabled=pager.status.terminal),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    def build_embed(self, pager: Pager) -> discord.Embed:
        embed = discord.Embed(
            title=f"📟 Pager #{pager.display_number}",
            description=_status_label(pager.status),
            color=_status_color(pager.status),
            timestamp=pager.created_at,
        )
        embed.add_field(name="Unidad emisora", value=pager.creator_callsign, inline=True)
        embed.add_field(name="Ubicación", value=pager.location, inline=False)
        embed.add_field(name="Situación", value=pager.situation, inline=False)
        responders = "\n".join(f"• **{item.callsign}** · <@{item.user_id}>" for item in pager.responders)
        embed.add_field(
            name=f"Unidades que responden · {len(pager.responders)}",
            value=responders or "Ninguna unidad registrada todavía.",
            inline=False,
        )
        embed.add_field(name="Emitido por", value=f"<@{pager.creator_user_id}>", inline=True)
        if pager.status.terminal:
            embed.add_field(
                name="Resultado" if pager.status is PagerStatus.CONCLUDED else "Motivo",
                value=pager.resolution or "Sin detalle.",
                inline=False,
            )
            if pager.closed_by_user_id:
                closed = discord.utils.format_dt(pager.closed_at or pager.updated_at, style="R")
                embed.add_field(
                    name="Cierre",
                    value=f"<@{pager.closed_by_user_id}> · {closed}",
                    inline=False,
                )
        embed.set_footer(text=f"Pager #{pager.display_number}")
        return embed

    @asynccontextmanager
    async def lock(self, pager_id: UUID):
        lock = self._locks.setdefault(pager_id, asyncio.Lock())
        self._lock_users[pager_id] = self._lock_users.get(pager_id, 0) + 1
        try:
            async with lock:
                yield
        finally:
            remaining = self._lock_users[pager_id] - 1
            if remaining:
                self._lock_users[pager_id] = remaining
            else:
                self._lock_users.pop(pager_id, None)
                self._locks.pop(pager_id, None)

    def _log_transition(self, event: str, pager: Pager, actor: int, transition: str) -> None:
        log_event(
            LOGGER,
            "info",
            event,
            guild_id=pager.guild_id,
            channel_id=pager.channel_id,
            message_id=pager.message_id,
            pager_id=str(pager.id),
            pager_number=pager.number,
            actor_user_id=actor,
            transition=transition,
            responder_count=len(pager.responders),
        )

    @staticmethod
    def _log_error(event: str, error: Exception, pager_id: UUID, interaction: discord.Interaction) -> None:
        log_event(
            LOGGER,
            "error",
            event,
            exc_info=error,
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            message_id=interaction.message.id if interaction.message else None,
            pager_id=str(pager_id),
            actor_user_id=interaction.user.id,
            **exception_fields(error),
        )


class PagerControlsView(discord.ui.View):
    def __init__(self, *, disabled: bool = False) -> None:
        super().__init__(timeout=None)
        if disabled:
            for child in self.children:
                child.disabled = True

    async def _service(self, interaction: discord.Interaction) -> PagerService | None:
        registry: PagerServiceRegistry = interaction.client.pager_registry  # type: ignore[attr-defined]
        if interaction.guild_id is None:
            return None
        try:
            return await registry.get(interaction.guild_id, require_enabled=False)
        except LookupError:
            return None

    @discord.ui.button(
        label="Responder / actualizar",
        emoji="📻",
        style=discord.ButtonStyle.primary,
        custom_id="pager:v1:respond",
    )
    async def respond_button(self, _button: discord.ui.Button, interaction: discord.Interaction) -> None:
        service = await self._service(interaction)
        if service is None or not service.enabled:
            await interaction.response.send_message(
                embed=_unavailable_embed(service),
                ephemeral=True,
            )
            return
        if not isinstance(interaction.user, discord.Member) or not service.can_respond(interaction.user):
            await interaction.response.send_message(
                embed=service.alert(service.callsign_ui.unauthorized),
                ephemeral=True,
            )
            return
        try:
            pager = await service.resolve_interaction_pager(interaction)
        except PagerValidationError as error:
            await interaction.response.send_message(embed=service.alert(error.public_message), ephemeral=True)
            return
        if pager.status is not PagerStatus.ACTIVE:
            await interaction.response.send_message(embed=service.alert(service.ui.stale), ephemeral=True)
            return
        if interaction.user.id == pager.creator_user_id:
            await interaction.response.send_message(
                embed=service.alert(service.ui.creator_cannot_respond), ephemeral=True
            )
            return
        try:
            callsign = await service.saved_callsign(interaction.user)
        except Exception as error:
            await interaction.response.send_message(
                embed=service.alert(service.callsign_ui.operation_failed),
                ephemeral=True,
            )
            service._log_error("pager_callsign_lookup_failed", error, pager.id, interaction)
            return
        if callsign is not None:
            await service.respond(interaction, pager.id, callsign)
            return
        await interaction.response.send_modal(PagerResponseModal(service, pager.id))

    @discord.ui.button(
        label="Retirarme",
        emoji="↩️",
        style=discord.ButtonStyle.secondary,
        custom_id="pager:v1:withdraw",
    )
    async def withdraw_button(self, _button: discord.ui.Button, interaction: discord.Interaction) -> None:
        service = await self._service(interaction)
        if service is None or not service.enabled:
            await interaction.response.send_message(
                embed=_unavailable_embed(service),
                ephemeral=True,
            )
            return
        await service.withdraw(interaction)

    @discord.ui.button(
        label="Editar",
        emoji="✏️",
        style=discord.ButtonStyle.secondary,
        custom_id="pager:v1:edit",
    )
    async def edit_button(self, _button: discord.ui.Button, interaction: discord.Interaction) -> None:
        service = await self._service(interaction)
        if service is None or not service.enabled:
            await interaction.response.send_message(
                embed=_unavailable_embed(service),
                ephemeral=True,
            )
            return
        try:
            pager = await service.resolve_interaction_pager(interaction)
        except PagerValidationError as error:
            await interaction.response.send_message(embed=service.alert(error.public_message), ephemeral=True)
            return
        if pager.status is not PagerStatus.ACTIVE:
            await interaction.response.send_message(embed=service.alert(service.ui.stale), ephemeral=True)
            return
        if not isinstance(interaction.user, discord.Member) or not service.can_edit(interaction.user, pager):
            await interaction.response.send_message(
                embed=service.alert(service.ui.unauthorized_editor), ephemeral=True
            )
            return
        if not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message(embed=service.alert(service.ui.stale), ephemeral=True)
            return
        roles = service.recipient_roles(interaction.user.guild)
        if not roles or len(roles) > 25:
            await interaction.response.send_message(
                embed=service.alert(service.ui.invalid_recipient), ephemeral=True
            )
            return
        await interaction.response.send_modal(
            PagerEditModal(service, pager, interaction.user, interaction.channel)
        )

    @discord.ui.button(
        label="Concluir",
        emoji="✅",
        style=discord.ButtonStyle.success,
        custom_id="pager:v1:conclude",
    )
    async def conclude_button(self, _button: discord.ui.Button, interaction: discord.Interaction) -> None:
        await self._open_close_modal(interaction, PagerStatus.CONCLUDED)

    @discord.ui.button(
        label="Cancelar",
        emoji="❌",
        style=discord.ButtonStyle.danger,
        custom_id="pager:v1:cancel",
    )
    async def cancel_button(self, _button: discord.ui.Button, interaction: discord.Interaction) -> None:
        await self._open_close_modal(interaction, PagerStatus.CANCELLED)

    async def _open_close_modal(
        self,
        interaction: discord.Interaction,
        status: PagerStatus,
    ) -> None:
        service = await self._service(interaction)
        if service is None or not service.enabled:
            await interaction.response.send_message(
                embed=_unavailable_embed(service),
                ephemeral=True,
            )
            return
        try:
            pager = await service.resolve_interaction_pager(interaction)
        except PagerValidationError as error:
            await interaction.response.send_message(embed=service.alert(error.public_message), ephemeral=True)
            return
        if not isinstance(interaction.user, discord.Member) or not service.can_close(interaction.user, pager):
            await interaction.response.send_message(
                embed=service.alert(service.ui.unauthorized_closer), ephemeral=True
            )
            return
        await interaction.response.send_modal(PagerCloseModal(service, pager.id, status))


class PagerCreateModal(discord.ui.DesignerModal):
    def __init__(
        self,
        service: PagerService,
        member: discord.Member,
        channel: discord.TextChannel,
        *,
        saved_callsign: str | None = None,
    ) -> None:
        self.service = service
        self.member = member
        self.channel = channel
        self.saved_callsign = saved_callsign
        roles = service.recipient_roles(member.guild)
        self.callsign: discord.ui.InputText | None = None
        fields: list[discord.ui.Item] = []
        if saved_callsign is None:
            self.callsign = discord.ui.InputText(
                custom_id="pager:v1:create:callsign",
                placeholder="Ej.: 241WS",
                min_length=1,
                max_length=40,
            )
            fields.append(discord.ui.Label("Unidad o callsign", self.callsign))
        self.recipient = discord.ui.StringSelect(
            custom_id="pager:v1:create:recipient",
            placeholder="Selecciona uno o más grupos destinatarios",
            options=[discord.SelectOption(label=role.name[:100], value=str(role.id)) for role in roles],
            min_values=1,
            max_values=len(roles),
        )
        self.location = discord.ui.InputText(
            custom_id="pager:v1:create:location",
            placeholder="Ubicación o referencia clara",
            min_length=1,
            max_length=200,
        )
        self.situation = discord.ui.InputText(
            custom_id="pager:v1:create:situation",
            style=discord.InputTextStyle.long,
            placeholder="Describe brevemente la situación y el apoyo requerido.",
            min_length=1,
            max_length=1000,
        )
        fields.extend(
            (
                discord.ui.Label("Destinatarios", self.recipient),
                discord.ui.Label("Ubicación", self.location),
                discord.ui.Label("Situación", self.situation),
            )
        )
        super().__init__(
            *fields,
            title="Emitir pager",
            custom_id=f"pager:v1:create:{member.id}",
            timeout=MODAL_TIMEOUT_SECONDS,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.user, discord.Member) or interaction.user.id != self.member.id:
            await interaction.response.send_message(
                embed=self.service.alert(self.service.ui.unavailable), ephemeral=True
            )
            return
        callsign = self.saved_callsign
        if callsign is None:
            if self.callsign is None:  # pragma: no cover - constructor invariant
                raise RuntimeError("Pager callsign input is unavailable")
            callsign = self.callsign.value
        await self.service.create(
            interaction,
            channel=self.channel,
            member=interaction.user,
            callsign=callsign,
            recipient_role_ids=tuple(int(role_id) for role_id in self.recipient.values),
            location=self.location.value,
            situation=self.situation.value,
        )


class PagerResponseModal(discord.ui.DesignerModal):
    def __init__(self, service: PagerService, pager_id: UUID) -> None:
        self.service = service
        self.pager_id = pager_id
        self.callsign = discord.ui.InputText(
            custom_id="pager:v1:response:callsign",
            placeholder="Ej.: 241WS",
            min_length=1,
            max_length=40,
        )
        super().__init__(
            discord.ui.Label("Unidad o callsign", self.callsign),
            title="Responder al pager",
            custom_id=f"pager:v1:response:{pager_id}",
            timeout=MODAL_TIMEOUT_SECONDS,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.service.respond(
            interaction,
            self.pager_id,
            self.callsign.value,
            remember=True,
        )


class PagerEditModal(discord.ui.DesignerModal):
    def __init__(
        self,
        service: PagerService,
        pager: Pager,
        member: discord.Member,
        channel: discord.TextChannel,
    ) -> None:
        self.service = service
        self.pager_id = pager.id
        self.editor_id = member.id
        self.channel = channel
        roles = service.recipient_roles(member.guild)
        selected = set(pager.recipient_role_ids)
        self.callsign = discord.ui.InputText(
            custom_id="pager:v1:edit:callsign",
            value=pager.creator_callsign,
            min_length=1,
            max_length=40,
        )
        self.recipient = discord.ui.StringSelect(
            custom_id="pager:v1:edit:recipient",
            placeholder="Selecciona uno o más grupos destinatarios",
            options=[
                discord.SelectOption(
                    label=role.name[:100],
                    value=str(role.id),
                    default=role.id in selected,
                )
                for role in roles
            ],
            min_values=1,
            max_values=len(roles),
        )
        self.location = discord.ui.InputText(
            custom_id="pager:v1:edit:location",
            value=pager.location,
            min_length=1,
            max_length=200,
        )
        self.situation = discord.ui.InputText(
            custom_id="pager:v1:edit:situation",
            style=discord.InputTextStyle.long,
            value=pager.situation,
            min_length=1,
            max_length=1000,
        )
        super().__init__(
            discord.ui.Label("Unidad o callsign", self.callsign),
            discord.ui.Label("Destinatarios", self.recipient),
            discord.ui.Label("Ubicación", self.location),
            discord.ui.Label("Situación", self.situation),
            title="Editar pager",
            custom_id=f"pager:v1:edit:{pager.id}",
            timeout=MODAL_TIMEOUT_SECONDS,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.user, discord.Member) or interaction.user.id != self.editor_id:
            await interaction.response.send_message(
                embed=self.service.alert(self.service.ui.unauthorized_editor), ephemeral=True
            )
            return
        await self.service.edit(
            interaction,
            self.pager_id,
            channel=self.channel,
            callsign=self.callsign.value,
            recipient_role_ids=tuple(int(role_id) for role_id in self.recipient.values),
            location=self.location.value,
            situation=self.situation.value,
        )


class PagerCloseModal(discord.ui.DesignerModal):
    def __init__(self, service: PagerService, pager_id: UUID, status: PagerStatus) -> None:
        self.service = service
        self.pager_id = pager_id
        self.status = status
        self.resolution = discord.ui.InputText(
            custom_id=f"pager:v1:{status.value}:resolution",
            style=discord.InputTextStyle.long,
            placeholder=(
                "Describe el resultado de la atención."
                if status is PagerStatus.CONCLUDED
                else "Indica el motivo de la cancelación."
            ),
            min_length=1,
            max_length=1000,
        )
        super().__init__(
            discord.ui.Label(
                "Resultado" if status is PagerStatus.CONCLUDED else "Motivo",
                self.resolution,
            ),
            title="Concluir pager" if status is PagerStatus.CONCLUDED else "Cancelar pager",
            custom_id=f"pager:v1:{status.value}:{pager_id}",
            timeout=MODAL_TIMEOUT_SECONDS,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.service.close(
            interaction,
            self.pager_id,
            status=self.status,
            resolution=self.resolution.value,
        )


class PagerListView(discord.ui.View):
    def __init__(
        self,
        service: PagerService,
        requester_id: int,
        *,
        personal: bool,
    ) -> None:
        super().__init__(timeout=LIST_TIMEOUT_SECONDS)
        self.service = service
        self.requester_id = requester_id
        self.personal = personal
        self.page = 0
        self.total = 0
        self._sync_buttons()

    async def render(self, guild_id: int) -> discord.Embed:
        offset = self.page * PAGE_SIZE
        if self.personal:
            entries, self.total = await self.service.repository.list_for_user(
                guild_id,
                self.requester_id,
                offset=offset,
                limit=PAGE_SIZE,
            )
            embed = _personal_list_embed(entries, self.total, self.page)
        else:
            entries, self.total = await self.service.repository.list_active(
                guild_id,
                offset=offset,
                limit=PAGE_SIZE,
            )
            embed = _active_list_embed(entries, self.total, self.page)
        self._sync_buttons()
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.requester_id:
            return True
        await interaction.response.send_message(
            "Solo quien abrió esta consulta puede cambiar de página.", ephemeral=True
        )
        return False

    @discord.ui.button(label="Anterior", emoji="◀️", style=discord.ButtonStyle.secondary)
    async def previous(self, _button: discord.ui.Button, interaction: discord.Interaction) -> None:
        self.page = max(0, self.page - 1)
        await interaction.response.edit_message(embed=await self.render(interaction.guild_id), view=self)

    @discord.ui.button(label="Siguiente", emoji="▶️", style=discord.ButtonStyle.secondary)
    async def next(self, _button: discord.ui.Button, interaction: discord.Interaction) -> None:
        pages = max(1, math.ceil(self.total / PAGE_SIZE))
        self.page = min(pages - 1, self.page + 1)
        await interaction.response.edit_message(embed=await self.render(interaction.guild_id), view=self)

    def _sync_buttons(self) -> None:
        pages = max(1, math.ceil(self.total / PAGE_SIZE))
        self.previous.disabled = self.page <= 0
        self.next.disabled = self.page >= pages - 1


class PagerCog(commands.Cog):
    def __init__(self, registry: PagerServiceRegistry, persona: BotPersona) -> None:
        self.registry = registry
        copy = persona.messages.interface.commands
        self.pager.description = copy.pager_description
        self.active_pagers.description = copy.active_pagers_description
        self.my_pagers.description = copy.my_pagers_description
        self.info_pager.description = copy.info_pager_description

    async def _service(
        self,
        ctx: discord.ApplicationContext,
        *,
        require_enabled: bool = True,
    ) -> PagerService | None:
        if ctx.guild is None:
            return None
        try:
            return await self.registry.get(ctx.guild.id, require_enabled=require_enabled)
        except LookupError:
            if require_enabled:
                try:
                    return await self.registry.get(ctx.guild.id, require_enabled=False)
                except LookupError:
                    pass
            return None

    @discord.slash_command(name="pager", description="Emite un pager operativo")
    async def pager(self, ctx: discord.ApplicationContext) -> None:
        service = await self._service(ctx)
        if service is None or not service.enabled:
            embed = (
                service.alert(service.ui.unavailable)
                if service
                else _generic_alert("El sistema de pagers no está habilitado.")
            )
            await ctx.respond(
                embed=embed,
                ephemeral=True,
            )
            return
        if not isinstance(ctx.author, discord.Member) or not service.can_create(ctx.author):
            await ctx.respond(embed=service.alert(service.ui.unauthorized_creator), ephemeral=True)
            return
        if not isinstance(ctx.channel, discord.TextChannel) or not service.channel_allowed(ctx.channel):
            channels = await service.allowed_channel_summary(ctx.guild)
            await ctx.respond(
                embed=service.alert(service.ui.wrong_channel.format(channels=channels)),
                ephemeral=True,
            )
            return
        roles = service.recipient_roles(ctx.guild)
        if (
            not roles
            or len(roles) > 25
            or any(not service.can_notify_role(ctx.channel, role) for role in roles)
        ):
            await ctx.respond(embed=service.alert(service.ui.invalid_recipient), ephemeral=True)
            return
        try:
            saved_callsign = await service.saved_callsign(ctx.author)
        except Exception as error:
            log_event(
                LOGGER,
                "error",
                "pager_creator_callsign_lookup_failed",
                exc_info=error,
                guild_id=ctx.guild.id,
                channel_id=ctx.channel.id,
                user_id=ctx.author.id,
                **exception_fields(error),
            )
            await ctx.respond(embed=service.alert(service.ui.operation_failed), ephemeral=True)
            return
        await ctx.send_modal(
            PagerCreateModal(
                service,
                ctx.author,
                ctx.channel,
                saved_callsign=saved_callsign,
            )
        )

    @discord.slash_command(name="pager-activos", description="Muestra los pagers activos")
    async def active_pagers(self, ctx: discord.ApplicationContext) -> None:
        service = await self._operational_service(ctx)
        if service is None:
            return
        view = PagerListView(service, ctx.author.id, personal=False)
        embed = await view.render(ctx.guild.id)
        await ctx.respond(
            embed=embed,
            view=view,
            ephemeral=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.slash_command(name="mis-pagers", description="Consulta sus pagers")
    async def my_pagers(self, ctx: discord.ApplicationContext) -> None:
        service = await self._operational_service(ctx)
        if service is None:
            return
        view = PagerListView(service, ctx.author.id, personal=True)
        embed = await view.render(ctx.guild.id)
        await ctx.respond(
            embed=embed,
            view=view,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.slash_command(name="info-pager", description="Explica el sistema de pagers")
    async def info_pager(self, ctx: discord.ApplicationContext) -> None:
        service = await self._service(ctx, require_enabled=False)
        if service is None or not service.enabled:
            await ctx.respond(
                embed=(
                    service.alert(service.ui.unavailable)
                    if service
                    else _generic_alert("Este servidor no tiene configuración de pager.")
                ),
                ephemeral=True,
            )
            return
        if not isinstance(ctx.author, discord.Member) or not service.can_view_info(ctx.author):
            await ctx.respond(
                embed=service.alert(service.ui.unauthorized_creator),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        channels = await service.allowed_channel_summary(ctx.guild)
        embed = discord.Embed(
            title="📻 Sistema de pagers",
            description=service.ui.info_intro,
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="Cómo emitir uno",
            value=(
                "Usa `/pager` y completa destinatarios, ubicación y situación. Si registraste "
                "`/callsign`, el bot lo utiliza automáticamente y no vuelve a pedirlo; si no, "
                "puedes indicarlo libremente en el formulario."
            ),
            inline=False,
        )
        embed.add_field(
            name="Responder y gestionar",
            value=(
                "Pulsa **Responder / actualizar**; si registraste `/callsign`, se reutiliza automáticamente. "
                "Si todavía no tienes uno, se solicitará y quedará guardado. Puedes retirarte después. "
                "El emisor o los responsables autorizados pueden editarlo, concluirlo o cancelarlo."
            ),
            inline=False,
        )
        embed.add_field(
            name="Consultas",
            value="`/pager-activos` muestra los abiertos y `/mis-pagers` tu historial privado.",
            inline=False,
        )
        embed.add_field(name="Canales habilitados", value=channels, inline=False)
        embed.add_field(name="Antes de emitir", value=service.ui.info_closing, inline=False)
        await ctx.respond(embed=embed, ephemeral=False, allowed_mentions=discord.AllowedMentions.none())

    async def _operational_service(self, ctx: discord.ApplicationContext) -> PagerService | None:
        service = await self._service(ctx)
        if service is None or not service.enabled:
            embed = (
                service.alert(service.ui.unavailable)
                if service
                else _generic_alert("El sistema de pagers no está habilitado.")
            )
            await ctx.respond(
                embed=embed,
                ephemeral=True,
            )
            return None
        if not isinstance(ctx.channel, discord.TextChannel) or not service.channel_allowed(ctx.channel):
            channels = await service.allowed_channel_summary(ctx.guild)
            await ctx.respond(
                embed=service.alert(service.ui.wrong_channel.format(channels=channels)),
                ephemeral=True,
            )
            return None
        return service


def _active_list_embed(entries: Iterable[Pager], total: int, page: int) -> discord.Embed:
    embed = discord.Embed(
        title="📻 Pagers activos",
        description="Avisos que todavía requieren seguimiento.",
        color=discord.Color.blurple(),
    )
    for pager in entries:
        link = f"[Abrir pager]({pager.jump_url})" if pager.jump_url else "Sin enlace"
        embed.add_field(
            name=f"#{pager.display_number} · {pager.location}",
            value=(
                f"{_recipient_mentions(pager)} · {discord.utils.format_dt(pager.created_at, style='R')}\n"
                f"**Unidades:** {len(pager.responders)} · {link}"
            ),
            inline=False,
        )
    if total == 0:
        embed.description = "No hay pagers activos en este momento."
    embed.set_footer(text=f"Página {page + 1}/{max(1, math.ceil(total / PAGE_SIZE))} · {total} activo(s)")
    return embed


def _personal_list_embed(entries: Iterable[UserPager], total: int, page: int) -> discord.Embed:
    embed = discord.Embed(
        title="📻 Mis pagers",
        description="Pagers que emitiste o en los que registraste una respuesta.",
        color=discord.Color.blurple(),
    )
    for item in entries:
        pager = item.pager
        link = f"[Abrir pager]({pager.jump_url})" if pager.jump_url else "Sin enlace"
        callsign = f" · **Callsign:** {item.responder_callsign}" if item.responder_callsign else ""
        embed.add_field(
            name=f"#{pager.display_number} · {_status_label(pager.status)}",
            value=f"**{item.relationship}**{callsign}\n{pager.location} · {link}",
            inline=False,
        )
    if total == 0:
        embed.description = "No tienes pagers registrados todavía."
    embed.set_footer(text=f"Página {page + 1}/{max(1, math.ceil(total / PAGE_SIZE))} · {total} resultado(s)")
    return embed


def _has_any_role(member: object, role_ids: tuple[int, ...]) -> bool:
    allowed = frozenset(role_ids)
    return any(getattr(role, "id", None) in allowed for role in getattr(member, "roles", ()))


def _status_label(status: PagerStatus) -> str:
    return {
        PagerStatus.PUBLISHING: "⏳ Publicando",
        PagerStatus.ACTIVE: "📻 Activo",
        PagerStatus.CONCLUDED: "✅ Concluido",
        PagerStatus.CANCELLED: "❌ Cancelado",
        PagerStatus.FAILED: "⚠️ Fallido",
    }[status]


def _status_color(status: PagerStatus) -> discord.Color:
    return {
        PagerStatus.PUBLISHING: discord.Color.gold(),
        PagerStatus.ACTIVE: discord.Color.blurple(),
        PagerStatus.CONCLUDED: discord.Color.green(),
        PagerStatus.CANCELLED: discord.Color.red(),
        PagerStatus.FAILED: discord.Color.dark_grey(),
    }[status]


def _recipient_mentions(pager: Pager) -> str:
    return " ".join(f"<@&{role_id}>" for role_id in pager.recipient_role_ids)


def _recipient_header(pager: Pager) -> str:
    mentions = " · ".join(f"<@&{role_id}>" for role_id in pager.recipient_role_ids)
    return f"📣 **Pager dirigido a**\n> {mentions}\n\n\u200b"


def _generic_alert(description: str) -> discord.Embed:
    return discord.Embed(title="⚠️ Pager no disponible", description=description, color=discord.Color.red())


def _unavailable_embed(service: PagerService | None) -> discord.Embed:
    return service.alert(service.ui.unavailable) if service else _generic_alert("Pager no disponible.")

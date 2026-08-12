"""Ephemeral Discord command for per-guild operational callsigns."""

from __future__ import annotations

import logging

import discord
from discord.ext import commands

from arrest_report_agent.config import BotPersona
from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.infrastructure.postgres import DatabaseConfigProvider

from .repository import MemberProfileRepository

LOGGER = logging.getLogger(__name__)
MODAL_TIMEOUT_SECONDS = 300


class CallsignCog(commands.Cog):
    """Register or replace the caller's reusable operational callsign."""

    def __init__(
        self,
        provider: DatabaseConfigProvider,
        repository: MemberProfileRepository,
        persona: BotPersona,
    ) -> None:
        self.provider = provider
        self.repository = repository
        self.default_persona = persona
        self.callsign.description = persona.messages.interface.commands.callsign_description

    @discord.slash_command(name="callsign", description="Registra su callsign operativo")
    async def callsign(self, ctx: discord.ApplicationContext) -> None:
        if ctx.guild is None or not isinstance(ctx.author, discord.Member):
            await ctx.respond(
                embed=_generic_alert("Este comando solo funciona dentro de un servidor."),
                ephemeral=True,
            )
            return
        try:
            bundle = await self.provider.cached(ctx.guild.id)
            ui = bundle.persona.profile.messages.interface.callsigns
            if not _member_has_any_role(
                ctx.author,
                bundle.settings.operational_staff_role_ids,
            ):
                await ctx.respond(embed=_alert(ui.alert_title, ui.unauthorized), ephemeral=True)
                return
            existing = await self.repository.get_callsign(ctx.guild.id, ctx.author.id)
            await ctx.send_modal(CallsignModal(self, ctx.author.id, existing))
        except Exception as error:
            log_event(
                LOGGER,
                "error",
                "callsign_command_failed",
                exc_info=error,
                guild_id=ctx.guild.id,
                user_id=ctx.author.id,
                **exception_fields(error),
            )
            try:
                bundle = await self.provider.cached(ctx.guild.id)
                ui = bundle.persona.profile.messages.interface.callsigns
                embed = _alert(ui.alert_title, ui.operation_failed)
            except Exception:
                embed = _generic_alert("No pude abrir el registro de callsign.")
            await ctx.respond(embed=embed, ephemeral=True)

    async def save(self, interaction: discord.Interaction, requester_id: int, callsign: str) -> None:
        if (
            interaction.guild is None
            or not isinstance(interaction.user, discord.Member)
            or interaction.user.id != requester_id
        ):
            await interaction.response.send_message(
                embed=_generic_alert("La solicitud ya no es válida."),
                ephemeral=True,
            )
            return
        try:
            bundle = await self.provider.cached(interaction.guild.id)
        except Exception as error:
            ui = self.default_persona.messages.interface.callsigns
            log_event(
                LOGGER,
                "error",
                "callsign_configuration_lookup_failed",
                exc_info=error,
                guild_id=interaction.guild.id,
                user_id=interaction.user.id,
                **exception_fields(error),
            )
            await interaction.response.send_message(
                embed=_alert(ui.alert_title, ui.operation_failed),
                ephemeral=True,
            )
            return
        ui = bundle.persona.profile.messages.interface.callsigns
        if not _member_has_any_role(interaction.user, bundle.settings.operational_staff_role_ids):
            await interaction.response.send_message(
                embed=_alert(ui.alert_title, ui.unauthorized),
                ephemeral=True,
            )
            return
        try:
            profile = await self.repository.upsert_callsign(
                interaction.guild.id,
                interaction.user.id,
                callsign,
            )
            await interaction.response.send_message(
                embed=discord.Embed(
                    title=ui.saved_title,
                    description=ui.saved_description.format(callsign=profile.callsign),
                    color=discord.Color.green(),
                ),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            log_event(
                LOGGER,
                "info",
                "member_callsign_updated",
                guild_id=interaction.guild.id,
                user_id=interaction.user.id,
            )
        except Exception as error:
            log_event(
                LOGGER,
                "error",
                "member_callsign_update_failed",
                exc_info=error,
                guild_id=interaction.guild.id,
                user_id=interaction.user.id,
                **exception_fields(error),
            )
            await interaction.response.send_message(
                embed=_alert(ui.alert_title, ui.operation_failed),
                ephemeral=True,
            )


class CallsignModal(discord.ui.DesignerModal):
    def __init__(self, cog: CallsignCog, requester_id: int, current: str | None) -> None:
        self.cog = cog
        self.requester_id = requester_id
        self.value = discord.ui.InputText(
            custom_id="callsign:v1:value",
            value=current,
            placeholder="Ej.: 241WS",
            min_length=1,
            max_length=40,
        )
        super().__init__(
            discord.ui.Label("Callsign", self.value),
            title="Configurar callsign",
            custom_id=f"callsign:v1:update:{requester_id}",
            timeout=MODAL_TIMEOUT_SECONDS,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog.save(interaction, self.requester_id, self.value.value or "")


def _member_has_any_role(member: discord.Member, role_ids: tuple[int, ...]) -> bool:
    allowed = set(role_ids)
    return bool(allowed and any(role.id in allowed for role in member.roles))


def _alert(title: str, description: str) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=discord.Color.red())


def _generic_alert(description: str) -> discord.Embed:
    return _alert("⚠️ Callsign no disponible", description)

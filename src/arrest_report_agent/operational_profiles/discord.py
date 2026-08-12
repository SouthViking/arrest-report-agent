"""Discord commands for per-guild operational callsigns."""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass

import discord
from discord.ext import commands

from arrest_report_agent.config import BotPersona, PersonaCallsignMessages
from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.infrastructure.postgres import DatabaseConfigProvider

from .models import CallsignFormatError, MemberProfile, normalize_callsign
from .repository import MemberProfileRepository

LOGGER = logging.getLogger(__name__)
MODAL_TIMEOUT_SECONDS = 300
LIST_TIMEOUT_SECONDS = 180
DIRECTORY_PAGE_SIZE = 8


@dataclass(frozen=True, slots=True)
class CallsignDirectoryGroup:
    callsign: str
    user_ids: tuple[int, ...]


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
        commands_copy = persona.messages.interface.commands
        self.callsign.description = commands_copy.callsign_description
        self.remove_callsign.description = commands_copy.remove_callsign_description
        self.callsigns.description = commands_copy.callsigns_description
        self.info_callsigns.description = commands_copy.info_callsigns_description

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
            if existing is not None:
                try:
                    existing = normalize_callsign(existing)
                except CallsignFormatError:
                    existing = None
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

    @discord.slash_command(name="quitar-callsign", description="Elimina su callsign guardado")
    async def remove_callsign(self, ctx: discord.ApplicationContext) -> None:
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
            removed = await self.repository.delete_callsign(ctx.guild.id, ctx.author.id)
            await ctx.respond(
                embed=discord.Embed(
                    title=ui.removed_title,
                    description=(ui.removed_description if removed else ui.missing_description),
                    color=discord.Color.green() if removed else discord.Color.blurple(),
                ),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            log_event(
                LOGGER,
                "info",
                "member_callsign_removed" if removed else "member_callsign_remove_noop",
                guild_id=ctx.guild.id,
                user_id=ctx.author.id,
            )
        except Exception as error:
            log_event(
                LOGGER,
                "error",
                "member_callsign_remove_failed",
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
                embed = _generic_alert("No pude quitar el callsign guardado.")
            await ctx.respond(embed=embed, ephemeral=True)

    @discord.slash_command(name="callsigns", description="Muestra el directorio de callsigns")
    async def callsigns(self, ctx: discord.ApplicationContext) -> None:
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
            profiles = await self.repository.list_for_guild(ctx.guild.id)
            groups = _directory_groups(ctx.guild, profiles)
            view = CallsignDirectoryView(ctx.author.id, groups, ui)
            await ctx.respond(
                embed=view.render(),
                view=view if len(groups) > DIRECTORY_PAGE_SIZE else None,
                ephemeral=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as error:
            log_event(
                LOGGER,
                "error",
                "callsign_directory_failed",
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
                embed = _generic_alert("No pude consultar el directorio de callsigns.")
            await ctx.respond(embed=embed, ephemeral=True)

    @discord.slash_command(name="info-callsigns", description="Explica el uso de los callsigns")
    async def info_callsigns(self, ctx: discord.ApplicationContext) -> None:
        if ctx.guild is None:
            await ctx.respond(
                embed=_generic_alert("Este comando solo funciona dentro de un servidor."),
                ephemeral=True,
            )
            return
        try:
            bundle = await self.provider.cached(ctx.guild.id)
            ui = bundle.persona.profile.messages.interface.callsigns
            embed = discord.Embed(
                title="📻 Callsigns operativos",
                description=ui.info_intro,
                color=discord.Color.blurple(),
            )
            embed.add_field(
                name="Registrar o actualizar",
                value=(
                    "Usa `/callsign` para guardar tu identificador operativo. "
                    "El formulario y su confirmación son privados. El formato es de dos o tres "
                    "números seguidos de `L`, `F`, `B` o `S`; por ejemplo `280L` o `60F`."
                ),
                inline=False,
            )
            embed.add_field(
                name="Dejar de reutilizarlo",
                value=(
                    "Usa `/quitar-callsign` para eliminar tu callsign guardado. "
                    "Desde entonces, los formularios de pager volverán a pedirte uno libre."
                ),
                inline=False,
            )
            embed.add_field(
                name="Uso en pagers",
                value=(
                    "Al emitir `/pager` o pulsar **Responder / actualizar**, el bot utiliza "
                    "automáticamente tu callsign guardado y omite ese campo. Si aún no tienes "
                    "uno, podrás indicarlo libremente en el formulario del pager."
                ),
                inline=False,
            )
            embed.add_field(
                name="Consultar el directorio",
                value=(
                    "Usa `/callsigns` para publicar el directorio agrupado por callsign, "
                    "sin notificar a los miembros."
                ),
                inline=False,
            )
            embed.add_field(name="Acceso", value=ui.info_closing, inline=False)
            await ctx.respond(
                embed=embed,
                ephemeral=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except Exception as error:
            log_event(
                LOGGER,
                "error",
                "callsign_info_failed",
                exc_info=error,
                guild_id=ctx.guild.id,
                user_id=ctx.author.id,
                **exception_fields(error),
            )
            await ctx.respond(
                embed=_generic_alert("No pude mostrar la información de callsigns."),
                ephemeral=True,
            )

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
            normalized_callsign = normalize_callsign(callsign)
            profile = await self.repository.upsert_callsign(
                interaction.guild.id,
                interaction.user.id,
                normalized_callsign,
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
        except CallsignFormatError:
            await interaction.response.send_message(
                embed=_alert(ui.alert_title, ui.invalid_format),
                ephemeral=True,
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
            placeholder="Ej.: 280L",
            min_length=3,
            max_length=4,
        )
        super().__init__(
            discord.ui.Label("Callsign", self.value),
            title="Configurar callsign",
            custom_id=f"callsign:v1:update:{requester_id}",
            timeout=MODAL_TIMEOUT_SECONDS,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog.save(interaction, self.requester_id, self.value.value or "")


class CallsignDirectoryView(discord.ui.View):
    def __init__(
        self,
        requester_id: int,
        groups: tuple[CallsignDirectoryGroup, ...],
        ui: PersonaCallsignMessages,
    ) -> None:
        super().__init__(timeout=LIST_TIMEOUT_SECONDS)
        self.requester_id = requester_id
        self.groups = groups
        self.ui = ui
        self.page = 0
        self._sync_buttons()

    def render(self) -> discord.Embed:
        member_count = sum(len(group.user_ids) for group in self.groups)
        embed = discord.Embed(
            title="📻 Directorio de callsigns",
            description=self.ui.directory_intro if self.groups else self.ui.directory_empty,
            color=discord.Color.blurple(),
        )
        start = self.page * DIRECTORY_PAGE_SIZE
        for group in self.groups[start : start + DIRECTORY_PAGE_SIZE]:
            embed.add_field(
                name=group.callsign,
                value=_member_mentions(group.user_ids),
                inline=False,
            )
        pages = max(1, math.ceil(len(self.groups) / DIRECTORY_PAGE_SIZE))
        embed.set_footer(
            text=(
                f"Página {self.page + 1}/{pages} · "
                f"{len(self.groups)} callsigns · {member_count} miembros"
            )
        )
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user is not None and interaction.user.id == self.requester_id:
            return True
        await interaction.response.send_message(
            embed=_alert(self.ui.alert_title, self.ui.navigation_owner_only),
            ephemeral=True,
        )
        return False

    @discord.ui.button(label="Anterior", emoji="◀️", style=discord.ButtonStyle.secondary)
    async def previous(
        self,
        _button: discord.ui.Button[CallsignDirectoryView],
        interaction: discord.Interaction,
    ) -> None:
        self.page = max(0, self.page - 1)
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.render(), view=self)

    @discord.ui.button(label="Siguiente", emoji="▶️", style=discord.ButtonStyle.secondary)
    async def next(
        self,
        _button: discord.ui.Button[CallsignDirectoryView],
        interaction: discord.Interaction,
    ) -> None:
        pages = max(1, math.ceil(len(self.groups) / DIRECTORY_PAGE_SIZE))
        self.page = min(pages - 1, self.page + 1)
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.render(), view=self)

    def _sync_buttons(self) -> None:
        pages = max(1, math.ceil(len(self.groups) / DIRECTORY_PAGE_SIZE))
        self.previous.disabled = self.page <= 0
        self.next.disabled = self.page >= pages - 1


def _directory_groups(
    guild: discord.Guild,
    profiles: tuple[MemberProfile, ...],
) -> tuple[CallsignDirectoryGroup, ...]:
    grouped: dict[str, tuple[str, list[discord.Member]]] = {}
    for profile in profiles:
        member = guild.get_member(profile.user_id)
        if member is None:
            continue
        key = " ".join(profile.callsign.split()).casefold()
        if key not in grouped:
            grouped[key] = (profile.callsign, [])
        grouped[key][1].append(member)
    result = [
        CallsignDirectoryGroup(
            callsign=callsign,
            user_ids=tuple(
                member.id
                for member in sorted(
                    members,
                    key=lambda item: (item.display_name.casefold(), item.id),
                )
            ),
        )
        for callsign, members in grouped.values()
    ]
    return tuple(sorted(result, key=lambda item: _natural_key(item.callsign)))


def _natural_key(value: str) -> tuple[tuple[int, int | str], ...]:
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part.casefold())
        for part in re.split(r"(\d+)", value)
        if part
    )


def _member_mentions(user_ids: tuple[int, ...]) -> str:
    lines: list[str] = []
    for position, user_id in enumerate(user_ids):
        candidate = f"<@{user_id}>"
        remaining = len(user_ids) - position
        suffix = f"\n… y {remaining} más" if remaining else ""
        if len("\n".join((*lines, candidate))) + len(suffix) > 1024:
            lines.append(f"… y {remaining} más")
            break
        lines.append(candidate)
    return "\n".join(lines)


def _member_has_any_role(member: discord.Member, role_ids: tuple[int, ...]) -> bool:
    allowed = set(role_ids)
    return bool(allowed and any(role.id in allowed for role in member.roles))


def _alert(title: str, description: str) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=discord.Color.red())


def _generic_alert(description: str) -> discord.Embed:
    return _alert("⚠️ Callsign no disponible", description)

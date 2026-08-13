"""Discord commands and private navigation for the deterministic suggestion inbox."""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from typing import cast

import discord
from discord.ext import commands

from arrest_report_agent.config import AppSettings, BotPersona, PersonaSuggestionMessages
from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.infrastructure.postgres import DatabaseConfigProvider

from .models import (
    Suggestion,
    SuggestionCategory,
    SuggestionCooldownError,
    SuggestionLimitReachedError,
    SuggestionNotFoundError,
    SuggestionStatus,
)
from .repository import MAX_PENDING_SUGGESTIONS, SuggestionRepository

LOGGER = logging.getLogger(__name__)
MODAL_TIMEOUT_SECONDS = 300
INBOX_TIMEOUT_SECONDS = 180
INBOX_PAGE_SIZE = 5
SUGGESTION_REFERENCE_PATTERN = re.compile(r"^SUG-([0-9]{1,10})$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class CategoryPresentation:
    label: str
    emoji: str


CATEGORY_PRESENTATION = {
    SuggestionCategory.AUTOMATION: CategoryPresentation("Bot y automatización", "🤖"),
    SuggestionCategory.PROCEDURE: CategoryPresentation("Procedimientos", "📋"),
    SuggestionCategory.DOCUMENTATION: CategoryPresentation("Guías y documentación", "📚"),
    SuggestionCategory.EXPERIENCE: CategoryPresentation("Experiencia y mejoras", "✨"),
    SuggestionCategory.CORRECTION: CategoryPresentation("Problema o corrección", "🐛"),
    SuggestionCategory.OTHER: CategoryPresentation("Otro", "🧩"),
}


class SuggestionCog(commands.Cog):
    """Accept public suggestions while keeping owner review completely private."""

    def __init__(
        self,
        settings: AppSettings,
        provider: DatabaseConfigProvider,
        repository: SuggestionRepository,
        persona: BotPersona,
    ) -> None:
        self.provider = provider
        self.repository = repository
        self.owner_ids = frozenset(settings.config_owner_user_ids)
        commands_copy = persona.messages.interface.commands
        self.create_suggestion.description = commands_copy.suggestion_description
        self.info_suggestions.description = commands_copy.info_suggestions_description
        self.suggestion_inbox.description = commands_copy.suggestions_description
        self.change_suggestion_status.description = commands_copy.suggestion_status_description

    async def _ui(self, guild_id: int) -> PersonaSuggestionMessages:
        bundle = await self.provider.cached(guild_id)
        return bundle.persona.profile.messages.interface.suggestions

    async def _persona(self, guild_id: int) -> BotPersona:
        return (await self.provider.cached(guild_id)).persona.profile

    def _is_owner(self, user_id: int) -> bool:
        return user_id in self.owner_ids

    @discord.slash_command(name="sugerencia", description="Envía una sugerencia privada")
    async def create_suggestion(self, ctx: discord.ApplicationContext) -> None:
        if ctx.guild is None or ctx.channel is None:
            await ctx.respond(
                embed=_generic_alert("Este comando solo funciona dentro de un servidor."),
                ephemeral=True,
            )
            return
        try:
            ui = await self._ui(ctx.guild.id)
            await ctx.send_modal(
                SuggestionCreateModal(
                    self,
                    requester_id=ctx.author.id,
                    guild_id=ctx.guild.id,
                    source_channel_id=ctx.channel.id,
                    ui=ui,
                )
            )
        except Exception as error:
            await self._command_failure(ctx, error, operation="open_submission")

    @discord.slash_command(name="info-sugerencias", description="Explica cómo enviar sugerencias")
    async def info_suggestions(self, ctx: discord.ApplicationContext) -> None:
        if ctx.guild is None:
            await ctx.respond(
                embed=_generic_alert("Este comando solo funciona dentro de un servidor."),
                ephemeral=True,
            )
            return
        try:
            ui = await self._ui(ctx.guild.id)
            await ctx.respond(
                embed=build_info_embed(ui),
                ephemeral=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            log_event(
                LOGGER,
                "info",
                "suggestion_info_shared",
                guild_id=ctx.guild.id,
                channel_id=ctx.channel.id if ctx.channel else "unknown",
                user_id=ctx.author.id,
            )
        except Exception as error:
            await self._command_failure(ctx, error, operation="info")

    @discord.slash_command(name="sugerencias", description="Abre el buzón privado de sugerencias")
    async def suggestion_inbox(self, ctx: discord.ApplicationContext) -> None:
        if ctx.guild is None:
            await ctx.respond(
                embed=_generic_alert("Este comando solo funciona dentro de un servidor."),
                ephemeral=True,
            )
            return
        try:
            ui = await self._ui(ctx.guild.id)
            if not self._is_owner(ctx.author.id):
                await ctx.respond(embed=_alert(ui.alert_title, ui.owner_unauthorized), ephemeral=True)
                return
            items, total = await self.repository.list_for_guild(
                ctx.guild.id,
                offset=0,
                limit=INBOX_PAGE_SIZE,
            )
            view = SuggestionInboxView(
                requester_id=ctx.author.id,
                guild_id=ctx.guild.id,
                repository=self.repository,
                ui=ui,
                items=items,
                total=total,
            )
            await ctx.respond(
                embed=view.render(),
                view=view if total else None,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            log_event(
                LOGGER,
                "info",
                "suggestion_inbox_opened",
                guild_id=ctx.guild.id,
                user_id=ctx.author.id,
                suggestion_count=total,
            )
        except Exception as error:
            await self._command_failure(ctx, error, operation="inbox")

    @discord.slash_command(
        name="estado-sugerencia",
        description="Actualiza en privado el estado de una sugerencia",
    )
    async def change_suggestion_status(self, ctx: discord.ApplicationContext) -> None:
        if ctx.guild is None:
            await ctx.respond(
                embed=_generic_alert("Este comando solo funciona dentro de un servidor."),
                ephemeral=True,
            )
            return
        try:
            ui = await self._ui(ctx.guild.id)
            if not self._is_owner(ctx.author.id):
                await ctx.respond(embed=_alert(ui.alert_title, ui.owner_unauthorized), ephemeral=True)
                return
            await ctx.send_modal(
                SuggestionStatusModal(
                    self,
                    requester_id=ctx.author.id,
                    guild_id=ctx.guild.id,
                    ui=ui,
                )
            )
        except Exception as error:
            await self._command_failure(ctx, error, operation="open_status")

    async def submit(
        self,
        interaction: discord.Interaction,
        *,
        requester_id: int,
        guild_id: int,
        source_channel_id: int,
        category: SuggestionCategory,
        title: str,
        description: str,
        ui: PersonaSuggestionMessages,
    ) -> None:
        user = interaction.user
        if (
            user is None
            or user.id != requester_id
            or interaction.guild is None
            or interaction.guild.id != guild_id
        ):
            await interaction.response.send_message(
                embed=_alert(ui.alert_title, ui.operation_failed),
                ephemeral=True,
            )
            return
        try:
            suggestion = await self.repository.submit(
                guild_id=guild_id,
                author_user_id=requester_id,
                source_channel_id=source_channel_id,
                category=category,
                title=title,
                description=description,
            )
            await interaction.response.send_message(
                embed=discord.Embed(
                    title=ui.submitted_title,
                    description=ui.submitted_description.format(suggestion_id=suggestion.reference),
                    color=discord.Color.green(),
                ),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            log_event(
                LOGGER,
                "info",
                "suggestion_submitted",
                guild_id=guild_id,
                channel_id=source_channel_id,
                user_id=requester_id,
                suggestion_id=str(suggestion.id),
                suggestion_number=suggestion.number,
                category=category.value,
            )
        except SuggestionLimitReachedError as error:
            await interaction.response.send_message(
                embed=_alert(
                    ui.alert_title,
                    ui.limit_reached.format(limit=error.limit),
                ),
                ephemeral=True,
            )
        except SuggestionCooldownError as error:
            await interaction.response.send_message(
                embed=_alert(
                    ui.alert_title,
                    ui.cooldown.format(seconds=error.remaining_seconds),
                ),
                ephemeral=True,
            )
        except Exception as error:
            log_event(
                LOGGER,
                "error",
                "suggestion_submission_failed",
                exc_info=error,
                guild_id=guild_id,
                channel_id=source_channel_id,
                user_id=requester_id,
                **exception_fields(error),
            )
            await interaction.response.send_message(
                embed=_alert(ui.alert_title, ui.operation_failed),
                ephemeral=True,
            )

    async def update_status(
        self,
        interaction: discord.Interaction,
        *,
        requester_id: int,
        guild_id: int,
        reference: str,
        status: SuggestionStatus,
        ui: PersonaSuggestionMessages,
    ) -> None:
        user = interaction.user
        if (
            user is None
            or user.id != requester_id
            or not self._is_owner(user.id)
            or interaction.guild is None
            or interaction.guild.id != guild_id
        ):
            await interaction.response.send_message(
                embed=_alert(ui.alert_title, ui.owner_unauthorized),
                ephemeral=True,
            )
            return
        normalized = reference.strip().upper()
        match = SUGGESTION_REFERENCE_PATTERN.fullmatch(normalized)
        if match is None:
            await interaction.response.send_message(
                embed=_alert(ui.alert_title, ui.invalid_reference),
                ephemeral=True,
            )
            return
        number = int(match.group(1))
        try:
            suggestion, changed = await self.repository.change_status(
                guild_id=guild_id,
                number=number,
                status=status,
                actor_user_id=requester_id,
            )
            status_label = _status_label(status, ui)
            description = (
                ui.status_updated_description if changed else ui.status_unchanged_description
            ).format(suggestion_id=suggestion.reference, status=status_label)
            await interaction.response.send_message(
                embed=discord.Embed(
                    title=ui.status_updated_title,
                    description=description,
                    color=(
                        discord.Color.green() if status is SuggestionStatus.ARCHIVED else discord.Color.gold()
                    ),
                ),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            log_event(
                LOGGER,
                "info",
                "suggestion_status_changed" if changed else "suggestion_status_unchanged",
                guild_id=guild_id,
                user_id=requester_id,
                suggestion_id=str(suggestion.id),
                suggestion_number=suggestion.number,
                status=status.value,
            )
        except SuggestionNotFoundError:
            await interaction.response.send_message(
                embed=_alert(
                    ui.alert_title,
                    ui.not_found.format(suggestion_id=normalized),
                ),
                ephemeral=True,
            )
        except SuggestionLimitReachedError as error:
            await interaction.response.send_message(
                embed=_alert(ui.alert_title, ui.limit_reached.format(limit=error.limit)),
                ephemeral=True,
            )
        except Exception as error:
            log_event(
                LOGGER,
                "error",
                "suggestion_status_change_failed",
                exc_info=error,
                guild_id=guild_id,
                user_id=requester_id,
                suggestion_reference=normalized,
                **exception_fields(error),
            )
            await interaction.response.send_message(
                embed=_alert(ui.alert_title, ui.operation_failed),
                ephemeral=True,
            )

    async def _command_failure(
        self,
        ctx: discord.ApplicationContext,
        error: Exception,
        *,
        operation: str,
    ) -> None:
        guild_id = ctx.guild.id if ctx.guild else None
        log_event(
            LOGGER,
            "error",
            "suggestion_command_failed",
            exc_info=error,
            guild_id=guild_id or "direct_message",
            user_id=ctx.author.id,
            operation=operation,
            **exception_fields(error),
        )
        try:
            ui = await self._ui(guild_id) if guild_id is not None else None
            embed = (
                _alert(ui.alert_title, ui.operation_failed)
                if ui is not None
                else _generic_alert("No pude completar la operación de sugerencias.")
            )
        except Exception:
            embed = _generic_alert("No pude completar la operación de sugerencias.")
        await ctx.respond(embed=embed, ephemeral=True)


class SuggestionCreateModal(discord.ui.DesignerModal):
    def __init__(
        self,
        cog: SuggestionCog,
        *,
        requester_id: int,
        guild_id: int,
        source_channel_id: int,
        ui: PersonaSuggestionMessages,
    ) -> None:
        self.cog = cog
        self.requester_id = requester_id
        self.guild_id = guild_id
        self.source_channel_id = source_channel_id
        self.ui = ui
        self.category = discord.ui.StringSelect(  # type: ignore[type-var]
            custom_id="suggestions:v1:create:category",
            placeholder="Selecciona una categoría",
            options=[
                discord.SelectOption(
                    label=presentation.label,
                    value=category.value,
                    emoji=presentation.emoji,
                )
                for category, presentation in CATEGORY_PRESENTATION.items()
            ],
            min_values=1,
            max_values=1,
        )
        self.title_input = discord.ui.InputText(
            custom_id="suggestions:v1:create:title",
            placeholder="Resume la idea en una línea",
            min_length=3,
            max_length=100,
        )
        self.description_input = discord.ui.InputText(
            custom_id="suggestions:v1:create:description",
            style=discord.InputTextStyle.long,
            placeholder="Explica qué propones y qué problema ayudaría a resolver.",
            min_length=10,
            max_length=1500,
        )
        super().__init__(
            discord.ui.Label("Categoría", self.category),
            discord.ui.Label("Título", self.title_input),
            discord.ui.Label("Sugerencia", self.description_input),
            title="Enviar una sugerencia",
            custom_id=f"suggestions:v1:create:{requester_id}",
            timeout=MODAL_TIMEOUT_SECONDS,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        assert self.category.values
        await self.cog.submit(
            interaction,
            requester_id=self.requester_id,
            guild_id=self.guild_id,
            source_channel_id=self.source_channel_id,
            category=SuggestionCategory(self.category.values[0]),
            title=self.title_input.value or "",
            description=self.description_input.value or "",
            ui=self.ui,
        )


class SuggestionStatusModal(discord.ui.DesignerModal):
    def __init__(
        self,
        cog: SuggestionCog,
        *,
        requester_id: int,
        guild_id: int,
        ui: PersonaSuggestionMessages,
    ) -> None:
        self.cog = cog
        self.requester_id = requester_id
        self.guild_id = guild_id
        self.ui = ui
        self.reference = discord.ui.InputText(
            custom_id="suggestions:v1:status:reference",
            placeholder="Ej.: SUG-00012",
            min_length=5,
            max_length=14,
        )
        self.status = discord.ui.StringSelect(  # type: ignore[type-var]
            custom_id="suggestions:v1:status:value",
            placeholder="Selecciona el nuevo estado",
            options=[
                discord.SelectOption(
                    label="Pendiente",
                    value=SuggestionStatus.PENDING.value,
                    emoji="⏳",
                ),
                discord.SelectOption(
                    label="Archivada",
                    value=SuggestionStatus.ARCHIVED.value,
                    emoji="🗃️",
                ),
            ],
            min_values=1,
            max_values=1,
        )
        super().__init__(
            discord.ui.Label("Identificador", self.reference),
            discord.ui.Label("Nuevo estado", self.status),
            title="Actualizar sugerencia",
            custom_id=f"suggestions:v1:status:{requester_id}",
            timeout=MODAL_TIMEOUT_SECONDS,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        assert self.status.values
        await self.cog.update_status(
            interaction,
            requester_id=self.requester_id,
            guild_id=self.guild_id,
            reference=self.reference.value or "",
            status=SuggestionStatus(self.status.values[0]),
            ui=self.ui,
        )


class SuggestionSelect(discord.ui.Select):
    def __init__(self, items: tuple[Suggestion, ...]) -> None:
        super().__init__(
            placeholder="Abrir una sugerencia",
            custom_id="suggestions:v1:inbox:select",
            options=[
                discord.SelectOption(
                    label=f"{item.reference} · {item.title}"[:100],
                    value=str(item.number),
                    description=_status_plain(item.status),
                    emoji=CATEGORY_PRESENTATION[item.category].emoji,
                )
                for item in items
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = cast(SuggestionInboxView, self.view)
        assert self.values
        selected = await view.repository.get_by_number(view.guild_id, int(self.values[0]))
        if selected is None:
            await interaction.response.send_message(
                embed=_alert(view.ui.alert_title, view.ui.operation_failed),
                ephemeral=True,
            )
            return
        view.selected = selected
        view._sync_controls()
        await interaction.response.edit_message(embed=view.render(), view=view)


class SuggestionInboxView(discord.ui.View):
    def __init__(
        self,
        *,
        requester_id: int,
        guild_id: int,
        repository: SuggestionRepository,
        ui: PersonaSuggestionMessages,
        items: tuple[Suggestion, ...],
        total: int,
    ) -> None:
        super().__init__(timeout=INBOX_TIMEOUT_SECONDS)
        self.requester_id = requester_id
        self.guild_id = guild_id
        self.repository = repository
        self.ui = ui
        self.items = items
        self.total = total
        self.page = 0
        self.selected: Suggestion | None = None
        self.select: SuggestionSelect | None = None
        self._sync_controls()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user is not None and interaction.user.id == self.requester_id:
            return True
        await interaction.response.send_message(
            embed=_alert(self.ui.alert_title, self.ui.inbox_owner_only),
            ephemeral=True,
        )
        return False

    def render(self) -> discord.Embed:
        if self.selected is not None:
            return build_detail_embed(self.selected, self.ui)
        return build_inbox_embed(
            self.items,
            total=self.total,
            page=self.page,
            ui=self.ui,
        )

    async def _load_page(self, page: int) -> None:
        pages = max(1, math.ceil(self.total / INBOX_PAGE_SIZE))
        self.page = min(max(0, page), pages - 1)
        self.items, self.total = await self.repository.list_for_guild(
            self.guild_id,
            offset=self.page * INBOX_PAGE_SIZE,
            limit=INBOX_PAGE_SIZE,
        )
        self.selected = None
        self._sync_controls()

    def _sync_controls(self) -> None:
        if self.select is not None:
            self.remove_item(self.select)
            self.select = None
        if self.items:
            self.select = SuggestionSelect(self.items)
            self.add_item(self.select)
        pages = max(1, math.ceil(self.total / INBOX_PAGE_SIZE))
        self.previous.disabled = self.page <= 0
        self.next.disabled = self.page >= pages - 1
        self.back.disabled = self.selected is None

    @discord.ui.button(label="Anterior", emoji="◀️", style=discord.ButtonStyle.secondary)
    async def previous(
        self,
        _button: discord.ui.Button[SuggestionInboxView],
        interaction: discord.Interaction,
    ) -> None:
        await self._load_page(self.page - 1)
        await interaction.response.edit_message(embed=self.render(), view=self)

    @discord.ui.button(label="Siguiente", emoji="▶️", style=discord.ButtonStyle.secondary)
    async def next(
        self,
        _button: discord.ui.Button[SuggestionInboxView],
        interaction: discord.Interaction,
    ) -> None:
        await self._load_page(self.page + 1)
        await interaction.response.edit_message(embed=self.render(), view=self)

    @discord.ui.button(label="Volver al listado", emoji="↩️", style=discord.ButtonStyle.secondary)
    async def back(
        self,
        _button: discord.ui.Button[SuggestionInboxView],
        interaction: discord.Interaction,
    ) -> None:
        self.selected = None
        self._sync_controls()
        await interaction.response.edit_message(embed=self.render(), view=self)


def build_info_embed(ui: PersonaSuggestionMessages) -> discord.Embed:
    embed = discord.Embed(
        title="💡 Enviar una sugerencia",
        description=ui.info_intro,
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Cómo enviarla", value=ui.info_usage, inline=False)
    embed.add_field(
        name="Categorías",
        value="\n".join(
            f"{presentation.emoji} {presentation.label}" for presentation in CATEGORY_PRESENTATION.values()
        ),
        inline=False,
    )
    embed.add_field(
        name="Límite",
        value=ui.info_limit.format(limit=MAX_PENDING_SUGGESTIONS),
        inline=False,
    )
    embed.add_field(name="⚠️ Uso responsable", value=ui.info_warning, inline=False)
    return embed


def build_inbox_embed(
    items: tuple[Suggestion, ...],
    *,
    total: int,
    page: int,
    ui: PersonaSuggestionMessages,
) -> discord.Embed:
    embed = discord.Embed(
        title="💡 Buzón de sugerencias",
        description=ui.inbox_intro if items else ui.inbox_empty,
        color=discord.Color.blurple(),
    )
    for item in items:
        presentation = CATEGORY_PRESENTATION[item.category]
        embed.add_field(
            name=f"{presentation.emoji} {item.reference} · {item.title}"[:256],
            value=(
                f"<@{item.author_user_id}> · "
                f"{discord.utils.format_dt(item.created_at, style='d')} · "
                f"{_status_label(item.status, ui)}"
            ),
            inline=False,
        )
    pages = max(1, math.ceil(total / INBOX_PAGE_SIZE))
    embed.set_footer(text=f"Página {page + 1}/{pages} · {total} sugerencias")
    return embed


def build_detail_embed(
    suggestion: Suggestion,
    ui: PersonaSuggestionMessages,
) -> discord.Embed:
    presentation = CATEGORY_PRESENTATION[suggestion.category]
    embed = discord.Embed(
        title=f"{presentation.emoji} {suggestion.reference} · {suggestion.title}"[:256],
        description=suggestion.description,
        color=(
            discord.Color.gold() if suggestion.status is SuggestionStatus.PENDING else discord.Color.green()
        ),
        timestamp=suggestion.created_at,
    )
    embed.add_field(name="Categoría", value=presentation.label, inline=True)
    embed.add_field(name="Estado", value=_status_label(suggestion.status, ui), inline=True)
    embed.add_field(name="Autor", value=f"<@{suggestion.author_user_id}>", inline=True)
    embed.add_field(name="Origen", value=f"<#{suggestion.source_channel_id}>", inline=True)
    embed.set_footer(text=f"Versión {suggestion.version}")
    return embed


def _status_label(status: SuggestionStatus, ui: PersonaSuggestionMessages) -> str:
    return ui.status_pending if status is SuggestionStatus.PENDING else ui.status_archived


def _status_plain(status: SuggestionStatus) -> str:
    return "Pendiente" if status is SuggestionStatus.PENDING else "Archivada"


def _alert(title: str, description: str) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=discord.Color.red())


def _generic_alert(description: str) -> discord.Embed:
    return _alert("⚠️ Sugerencias no disponibles", description)

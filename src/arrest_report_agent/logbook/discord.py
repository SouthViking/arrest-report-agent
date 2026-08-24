"""Channel-scoped Discord workflow for structured logbook entries."""

from __future__ import annotations

import io
import logging
from contextlib import suppress
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import cast

import discord
from discord.ext import commands

from arrest_report_agent.application.date_ranges import DateRangePeriod, resolve_date_range
from arrest_report_agent.config import BotPersona, PersonaLogbookMessages
from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.infrastructure.postgres import DatabaseConfigProvider

from .models import (
    CATEGORY_SUBTYPES,
    LogbookCategory,
    LogbookEntry,
    LogbookSummary,
    MoneyDirection,
    available_subtypes,
    subtype_label,
)
from .service import LogbookFile, LogbookFileLimitError, LogbookService, safe_filename

LOGGER = logging.getLogger(__name__)
MODAL_TIMEOUT_SECONDS = 300
SUMMARY_MAX_RANGE_DAYS = 366
SUMMARY_MAX_SUBTYPES = 10


class LogbookSummaryVisibility(StrEnum):
    PUBLIC = "public"
    PRIVATE = "private"


@dataclass(frozen=True, slots=True)
class CategoryPresentation:
    label: str
    emoji: str
    color: int
    subject_label: str


CATEGORY_PRESENTATION: dict[LogbookCategory, CategoryPresentation] = {
    LogbookCategory.FINANCE: CategoryPresentation("Finanzas", "💵", 0x2ECC71, "Concepto u operación"),
    LogbookCategory.PERSONNEL: CategoryPresentation("Personal", "👤", 0x3498DB, "Miembro involucrado"),
    LogbookCategory.ASSIGNMENTS: CategoryPresentation(
        "Rangos y asignaciones", "🎖️", 0xF1C40F, "Miembro o unidad"
    ),
    LogbookCategory.EQUIPMENT: CategoryPresentation(
        "Licencias y equipo", "🪪", 0x9B59B6, "Elemento o responsable"
    ),
    LogbookCategory.ORDERS: CategoryPresentation(
        "Órdenes y procedimientos", "📋", 0xE67E22, "Persona o procedimiento"
    ),
    LogbookCategory.EVIDENCE: CategoryPresentation("Evidencia", "📦", 0x1ABC9C, "Evidencia o referencia"),
    LogbookCategory.FLEET: CategoryPresentation(
        "Flota e infraestructura", "🚓", 0x5865F2, "Vehículo, bien o instalación"
    ),
    LogbookCategory.OTHER: CategoryPresentation("Otro", "📝", 0x95A5A6, "Asunto"),
}


class LogbookCog(commands.Cog):
    """Publish logbook entries only from explicitly configured channels."""

    def __init__(
        self,
        provider: DatabaseConfigProvider,
        service: LogbookService,
        persona: BotPersona,
    ) -> None:
        self.provider = provider
        self.service = service
        self.default_persona = persona
        commands_copy = persona.messages.interface.commands
        self.create_entry.description = commands_copy.logbook_description
        self.info.description = commands_copy.info_logbook_description

    @discord.slash_command(name="bitacora", description="Registra una novedad estructurada")
    async def create_entry(self, ctx: discord.ApplicationContext) -> None:
        if ctx.guild is None or ctx.channel is None:
            await ctx.respond(
                embed=_generic_alert("Este comando solo funciona en un servidor."), ephemeral=True
            )
            return
        try:
            bundle = await self.provider.cached(ctx.guild.id)
            ui = bundle.persona.profile.messages.interface.logbook
            denial = _availability_denial(bundle, ctx.channel.id, ui)
            if denial is not None:
                await ctx.respond(embed=denial, ephemeral=True)
                return
            await ctx.respond(
                embed=discord.Embed(
                    title="📝 Nueva entrada de bitácora",
                    description=ui.setup_description,
                    color=discord.Color.blurple(),
                ),
                view=LogbookSetupView(
                    cog=self,
                    requester_id=ctx.author.id,
                    guild_id=ctx.guild.id,
                    channel_id=ctx.channel.id,
                ),
                ephemeral=True,
            )
        except Exception as error:
            await self._command_failure(ctx, error, operation="open")

    @discord.slash_command(name="info-bitacora", description="Explica cómo usar la bitácora")
    async def info(self, ctx: discord.ApplicationContext) -> None:
        if ctx.guild is None or ctx.channel is None:
            await ctx.respond(
                embed=_generic_alert("Este comando solo funciona en un servidor."), ephemeral=True
            )
            return
        try:
            bundle = await self.provider.cached(ctx.guild.id)
            ui = bundle.persona.profile.messages.interface.logbook
            denial = _availability_denial(bundle, ctx.channel.id, ui)
            if denial is not None:
                await ctx.respond(embed=denial, ephemeral=True)
                return
            await ctx.respond(
                embed=build_info_embed(ui),
                ephemeral=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            log_event(
                LOGGER,
                "info",
                "logbook_info_shared",
                guild_id=ctx.guild.id,
                channel_id=ctx.channel.id,
                user_id=ctx.author.id,
            )
        except Exception as error:
            await self._command_failure(ctx, error, operation="info")

    @discord.slash_command(
        name="guia-bitacora",
        description="Muestra las categorías y tipos disponibles en la bitácora",
    )
    async def guide(self, ctx: discord.ApplicationContext) -> None:
        if ctx.guild is None or ctx.channel is None:
            await ctx.respond(
                embed=_generic_alert("Este comando solo funciona en un servidor."), ephemeral=True
            )
            return
        try:
            bundle = await self.provider.cached(ctx.guild.id)
            ui = bundle.persona.profile.messages.interface.logbook
            denial = _availability_denial(bundle, ctx.channel.id, ui)
            if denial is not None:
                await ctx.respond(embed=denial, ephemeral=True)
                return
            await ctx.send_modal(
                LogbookGuideModal(
                    cog=self,
                    requester_id=ctx.author.id,
                    guild_id=ctx.guild.id,
                    channel_id=ctx.channel.id,
                )
            )
            log_event(
                LOGGER,
                "info",
                "logbook_guide_opened",
                guild_id=ctx.guild.id,
                channel_id=ctx.channel.id,
                user_id=ctx.author.id,
            )
        except Exception as error:
            await self._command_failure(ctx, error, operation="guide")

    async def show_guide(
        self,
        interaction: discord.Interaction,
        *,
        requester_id: int,
        guild_id: int,
        channel_id: int,
        publish: bool,
    ) -> None:
        if not await _validate_modal_context(
            interaction,
            self,
            requester_id,
            guild_id,
            channel_id,
        ):
            return
        bundle = await self.provider.cached(guild_id)
        ui = bundle.persona.profile.messages.interface.logbook
        await interaction.response.send_message(
            embed=build_guide_embed(ui),
            ephemeral=not publish,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        log_event(
            LOGGER,
            "info",
            "logbook_guide_shared",
            guild_id=guild_id,
            channel_id=channel_id,
            user_id=requester_id,
            visibility="public" if publish else "private",
        )

    @discord.slash_command(
        name="resumen-bitacora",
        description="Resume los registros de bitácora por período y tipo",
    )
    async def summary(self, ctx: discord.ApplicationContext) -> None:
        if (
            ctx.guild is None
            or ctx.channel is None
            or not isinstance(ctx.author, discord.Member)
        ):
            await ctx.respond(
                embed=_generic_alert("Este comando solo funciona en un servidor."),
                ephemeral=True,
            )
            return
        try:
            bundle = await self.provider.cached(ctx.guild.id)
            logbook_ui = bundle.persona.profile.messages.interface.logbook
            report_ui = bundle.persona.profile.messages.interface.faction_reports
            feature = bundle.snapshot.features.get("logbook")
            if feature is None or not feature.enabled:
                await ctx.respond(
                    embed=_alert(logbook_ui.alert_title, logbook_ui.unavailable),
                    ephemeral=True,
                )
                return
            if not _member_has_any_role(ctx.author, bundle.settings.faction_report_role_ids):
                await ctx.respond(
                    embed=_alert(logbook_ui.alert_title, report_ui.unauthorized),
                    ephemeral=True,
                )
                return
            await ctx.respond(
                embed=discord.Embed(
                    title="📋 Resumen de bitácora",
                    description=(
                        "Seleccione una categoría y, si corresponde, un tipo específico. "
                        "Después podrá definir el período y la visibilidad."
                    ),
                    color=discord.Color.blurple(),
                ),
                view=LogbookSummarySetupView(
                    cog=self,
                    requester_id=ctx.author.id,
                    guild_id=ctx.guild.id,
                    channel_id=ctx.channel.id,
                ),
                ephemeral=True,
            )
        except Exception as error:
            await self._command_failure(ctx, error, operation="summary_open")

    async def publish_summary(
        self,
        interaction: discord.Interaction,
        *,
        requester_id: int,
        guild_id: int,
        channel_id: int,
        period: DateRangePeriod,
        raw_date_from: str,
        raw_date_to: str,
        category: LogbookCategory | None,
        subtype: str | None,
        visibility: LogbookSummaryVisibility,
    ) -> None:
        if (
            interaction.guild is None
            or interaction.guild.id != guild_id
            or interaction.channel is None
            or interaction.channel.id != channel_id
            or not isinstance(interaction.user, discord.Member)
            or interaction.user.id != requester_id
        ):
            await interaction.response.send_message(
                embed=_generic_alert("Esta consulta ya no es válida."),
                ephemeral=True,
            )
            return
        bundle = await self.provider.cached(guild_id)
        logbook_ui = bundle.persona.profile.messages.interface.logbook
        report_ui = bundle.persona.profile.messages.interface.faction_reports
        feature = bundle.snapshot.features.get("logbook")
        if feature is None or not feature.enabled:
            await interaction.response.send_message(
                embed=_alert(logbook_ui.alert_title, logbook_ui.unavailable),
                ephemeral=True,
            )
            return
        if not _member_has_any_role(
            interaction.user,
            bundle.settings.faction_report_role_ids,
        ):
            await interaction.response.send_message(
                embed=_alert(logbook_ui.alert_title, report_ui.unauthorized),
                ephemeral=True,
            )
            return
        try:
            date_from, date_to = resolve_date_range(
                period,
                raw_date_from,
                raw_date_to,
                max_days=SUMMARY_MAX_RANGE_DAYS,
            )
        except ValueError:
            await interaction.response.send_message(
                embed=_alert(logbook_ui.alert_title, report_ui.invalid_range),
                ephemeral=True,
            )
            return
        private = visibility is LogbookSummaryVisibility.PRIVATE
        await interaction.response.defer(ephemeral=private)
        try:
            result = await self.service.summarize(
                guild_id=guild_id,
                date_from=date_from,
                date_to=date_to,
                category=category,
                subtype=subtype,
            )
            await interaction.followup.send(
                embed=build_summary_embed(result),
                ephemeral=private,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            log_event(
                LOGGER,
                "info",
                "logbook_summary_published",
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=requester_id,
                date_from=date_from.isoformat(),
                date_to=date_to.isoformat(),
                category=category.value if category is not None else "all",
                subtype=subtype or "all",
                visibility=visibility.value,
                total_entries=result.total_entries,
                attachment_count=result.attachment_count,
            )
        except Exception as error:
            log_event(
                LOGGER,
                "error",
                "logbook_summary_failed",
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=requester_id,
                date_from=date_from.isoformat(),
                date_to=date_to.isoformat(),
                category=category.value if category is not None else "all",
                subtype=subtype or "all",
                exc_info=error,
                **exception_fields(error),
            )
            await interaction.followup.send(
                embed=_alert(logbook_ui.alert_title, report_ui.operation_failed),
                ephemeral=private,
            )

    async def publish(
        self,
        interaction: discord.Interaction,
        *,
        requester_id: int,
        guild_id: int,
        channel_id: int,
        category: LogbookCategory,
        money_direction: MoneyDirection | None,
        subtype: str,
        subject: str,
        description: str,
        amount_text: str,
        attachments: tuple[discord.Attachment, ...],
    ) -> None:
        if (
            interaction.user is None
            or interaction.user.id != requester_id
            or interaction.guild is None
            or interaction.guild.id != guild_id
            or interaction.channel is None
            or interaction.channel.id != channel_id
        ):
            await interaction.response.send_message(
                embed=_generic_alert("Este formulario ya no es válido."),
                ephemeral=True,
            )
            return
        bundle = await self.provider.cached(guild_id)
        ui = bundle.persona.profile.messages.interface.logbook
        denial = _availability_denial(bundle, channel_id, ui)
        if denial is not None:
            await interaction.response.send_message(embed=denial, ephemeral=True)
            return
        normalized_subject = subject.strip()
        normalized_description = description.strip()
        if not normalized_subject or not normalized_description:
            await interaction.response.send_message(
                embed=_alert(ui.alert_title, ui.operation_failed), ephemeral=True
            )
            return
        try:
            money_amount = parse_money_amount(amount_text) if money_direction is not None else None
        except ValueError:
            await interaction.response.send_message(
                embed=_alert(ui.alert_title, ui.invalid_amount), ephemeral=True
            )
            return
        if (
            len(attachments) > 10
            or any(item.size <= 0 or item.size > bundle.settings.max_file_bytes for item in attachments)
            or sum(item.size for item in attachments) > bundle.settings.max_total_file_bytes
        ):
            await interaction.response.send_message(
                embed=_alert(ui.alert_title, ui.invalid_files), ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        entry: LogbookEntry | None = None
        try:
            entry, created = await self.service.repository.reserve(
                guild_id=guild_id,
                channel_id=channel_id,
                interaction_id=interaction.id,
                author_user_id=requester_id,
                category=category,
                subtype=subtype,
                subject=normalized_subject,
                description=normalized_description,
                money_direction=money_direction,
                money_amount=money_amount,
                money_detail=(normalized_subject if money_direction is not None else None),
            )
            if not created:
                await interaction.edit_original_response(
                    embed=_alert(ui.alert_title, "Esta novedad ya fue procesada."),
                )
                return
            loaded_files: list[LogbookFile] = []
            for attachment in attachments:
                loaded_files.append(
                    LogbookFile(
                        filename=attachment.filename,
                        content_type=attachment.content_type,
                        payload=await attachment.read(),
                    )
                )
            files = tuple(loaded_files)
            stored_assets = await self.service.store_files(
                entry,
                files,
                max_file_bytes=bundle.settings.max_file_bytes,
                max_total_bytes=bundle.settings.max_total_file_bytes,
            )
            output_files = _discord_files(files)
            embed = build_entry_embed(entry, attachment_count=len(stored_assets))
            if output_files and files[0].content_type and files[0].content_type.startswith("image/"):
                embed.set_image(url=f"attachment://{output_files[0].filename}")
            message = await interaction.channel.send(
                embed=embed,
                files=list(output_files),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            await self.service.repository.mark_published(entry.id, message.id)
            with suppress(discord.HTTPException):
                await interaction.delete_original_response()
            log_event(
                LOGGER,
                "info",
                "logbook_entry_published",
                guild_id=guild_id,
                channel_id=channel_id,
                message_id=message.id,
                entry_id=str(entry.id),
                entry_number=entry.number,
                user_id=requester_id,
                category=entry.category.value,
                subtype=entry.subtype,
                has_money=entry.money_direction is not None,
                attachment_count=len(stored_assets),
            )
        except LogbookFileLimitError as error:
            if entry is not None:
                with suppress(Exception):
                    await self.service.repository.mark_failed(entry.id)
            await interaction.edit_original_response(embed=_alert(ui.alert_title, ui.invalid_files))
            log_event(
                LOGGER,
                "warning",
                "logbook_files_rejected",
                guild_id=guild_id,
                user_id=requester_id,
                error_type=type(error).__name__,
            )
        except Exception as error:
            if entry is not None:
                with suppress(Exception):
                    await self.service.repository.mark_failed(entry.id)
            await interaction.edit_original_response(embed=_alert(ui.alert_title, ui.operation_failed))
            log_event(
                LOGGER,
                "error",
                "logbook_publish_failed",
                guild_id=guild_id,
                channel_id=channel_id,
                user_id=requester_id,
                entry_id=str(entry.id) if entry is not None else None,
                exc_info=error,
                **exception_fields(error),
            )

    async def _command_failure(
        self,
        ctx: discord.ApplicationContext,
        error: Exception,
        *,
        operation: str,
    ) -> None:
        ui = self.default_persona.messages.interface.logbook
        description = (
            self.default_persona.messages.interface.faction_reports.operation_failed
            if operation.startswith("summary")
            else ui.operation_failed
        )
        log_event(
            LOGGER,
            "error",
            "logbook_command_failed",
            operation=operation,
            guild_id=ctx.guild.id if ctx.guild else "direct_message",
            channel_id=ctx.channel.id if ctx.channel else "unknown",
            user_id=ctx.author.id,
            exc_info=error,
            **exception_fields(error),
        )
        await ctx.respond(embed=_alert(ui.alert_title, description), ephemeral=True)


class LogbookSummaryCategorySelect(discord.ui.Select["LogbookSummarySetupView"]):
    def __init__(self, selected: LogbookCategory | None = None) -> None:
        super().__init__(
            custom_id="logbook_summary:v1:category",
            placeholder=(
                CATEGORY_PRESENTATION[selected].label
                if selected is not None
                else "Todas las categorías"
            ),
            options=[
                discord.SelectOption(
                    label="Todas las categorías",
                    value="all",
                    default=selected is None,
                ),
                *(
                    discord.SelectOption(
                        label=presentation.label,
                        value=category.value,
                        emoji=presentation.emoji,
                        default=category is selected,
                    )
                    for category, presentation in CATEGORY_PRESENTATION.items()
                ),
            ],
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = cast(LogbookSummarySetupView, self.view)
        selected = next(iter(self.values or ()), "all")
        view.select_category(None if selected == "all" else LogbookCategory(selected))
        await interaction.response.edit_message(view=view)


class LogbookSummarySubtypeSelect(discord.ui.Select["LogbookSummarySetupView"]):
    def __init__(self, category: LogbookCategory | None = None) -> None:
        options = [discord.SelectOption(label="Todos los tipos", value="all", default=True)]
        if category is not None:
            options.extend(
                discord.SelectOption(label=label, value=key)
                for key, label in CATEGORY_SUBTYPES[category]
            )
        super().__init__(
            custom_id="logbook_summary:v1:subtype",
            placeholder=(
                "Todos los tipos"
                if category is not None
                else "Seleccione una categoría para filtrar por tipo"
            ),
            options=options,
            disabled=category is None,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = cast(LogbookSummarySetupView, self.view)
        selected = next(iter(self.values or ()), "all")
        view.subtype_filter = None if selected == "all" else selected
        await interaction.response.defer(invisible=True)


class LogbookSummaryContinueButton(discord.ui.Button["LogbookSummarySetupView"]):
    def __init__(self) -> None:
        super().__init__(
            label="Definir período",
            emoji="➡️",
            style=discord.ButtonStyle.primary,
            custom_id="logbook_summary:v1:continue",
            row=2,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = cast(LogbookSummarySetupView, self.view)
        await view.open_modal(interaction)


class LogbookSummarySetupView(discord.ui.View):
    def __init__(
        self,
        *,
        cog: LogbookCog,
        requester_id: int,
        guild_id: int,
        channel_id: int,
    ) -> None:
        super().__init__(timeout=MODAL_TIMEOUT_SECONDS, disable_on_timeout=True)
        self.cog = cog
        self.requester_id = requester_id
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.category_filter: LogbookCategory | None = None
        self.subtype_filter: str | None = None
        self.category = LogbookSummaryCategorySelect()
        self.subtype = LogbookSummarySubtypeSelect()
        self.add_item(self.category)
        self.add_item(self.subtype)
        self.add_item(LogbookSummaryContinueButton())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user is None or interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                embed=_generic_alert("Este selector pertenece a otra persona."),
                ephemeral=True,
            )
            return False
        return True

    def select_category(self, category: LogbookCategory | None) -> None:
        self.category_filter = category
        self.subtype_filter = None
        self.remove_item(self.category)
        self.remove_item(self.subtype)
        self.category = LogbookSummaryCategorySelect(category)
        self.subtype = LogbookSummarySubtypeSelect(category)
        self.add_item(self.category)
        self.add_item(self.subtype)

    async def open_modal(self, interaction: discord.Interaction) -> None:
        if not await _validate_summary_context(
            interaction,
            self.cog,
            self.requester_id,
            self.guild_id,
            self.channel_id,
        ):
            return
        await interaction.response.send_modal(
            LogbookSummaryModal(
                cog=self.cog,
                requester_id=self.requester_id,
                guild_id=self.guild_id,
                channel_id=self.channel_id,
                category=self.category_filter,
                subtype=self.subtype_filter,
            )
        )
        self.stop()
        with suppress(discord.HTTPException):
            await interaction.delete_original_response()


class LogbookSummaryModal(discord.ui.DesignerModal):
    def __init__(
        self,
        *,
        cog: LogbookCog,
        requester_id: int,
        guild_id: int,
        channel_id: int,
        category: LogbookCategory | None,
        subtype: str | None,
    ) -> None:
        self.cog = cog
        self.requester_id = requester_id
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.category = category
        self.subtype = subtype
        self.period = discord.ui.StringSelect(  # type: ignore[type-var]
            custom_id="logbook_summary:v1:period",
            options=[
                discord.SelectOption(label="Último día", value="1"),
                discord.SelectOption(label="Últimos 2 días", value="2"),
                discord.SelectOption(label="Últimos 7 días", value="7", default=True),
                discord.SelectOption(label="Últimos 14 días", value="14"),
                discord.SelectOption(label="Últimos 30 días", value="30"),
                discord.SelectOption(label="Últimos 90 días", value="90"),
                discord.SelectOption(label="Rango personalizado", value="custom"),
            ],
            min_values=1,
            max_values=1,
        )
        self.date_from = discord.ui.InputText(
            custom_id="logbook_summary:v1:date_from",
            placeholder="AAAA-MM-DD",
            required=False,
            min_length=10,
            max_length=10,
        )
        self.date_to = discord.ui.InputText(
            custom_id="logbook_summary:v1:date_to",
            placeholder="AAAA-MM-DD",
            required=False,
            min_length=10,
            max_length=10,
        )
        self.publish = discord.ui.Checkbox(
            custom_id="logbook_summary:v1:publish",
            default=True,
        )
        super().__init__(
            discord.ui.Label("Período", self.period),
            discord.ui.Label("Desde · solo para rango personalizado", self.date_from),
            discord.ui.Label("Hasta · solo para rango personalizado", self.date_to),
            discord.ui.Label("Publicar el resumen en este canal", self.publish),
            title="Resumir bitácora",
            custom_id=f"logbook_summary:v1:run:{requester_id}",
            timeout=MODAL_TIMEOUT_SECONDS,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        period = next(iter(self.period.values or ()), DateRangePeriod.LAST_7_DAYS.value)
        await self.cog.publish_summary(
            interaction,
            requester_id=self.requester_id,
            guild_id=self.guild_id,
            channel_id=self.channel_id,
            period=DateRangePeriod(period),
            raw_date_from=self.date_from.value or "",
            raw_date_to=self.date_to.value or "",
            category=self.category,
            subtype=self.subtype,
            visibility=(
                LogbookSummaryVisibility.PUBLIC
                if self.publish.value
                else LogbookSummaryVisibility.PRIVATE
            ),
        )


class LogbookCategorySelect(discord.ui.Select["LogbookSetupView"]):
    def __init__(self) -> None:
        super().__init__(
            custom_id="logbook:v1:category",
            placeholder="Selecciona la categoría",
            options=[
                discord.SelectOption(label=item.label, value=category.value, emoji=item.emoji)
                for category, item in CATEGORY_PRESENTATION.items()
            ],
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(invisible=True)


class LogbookMoneySelect(discord.ui.Select["LogbookSetupView"]):
    def __init__(self) -> None:
        super().__init__(
            custom_id="logbook:v1:money",
            placeholder="Movimiento de dinero",
            options=[
                discord.SelectOption(
                    label="Sin movimiento de dinero", value="none", default=True, emoji="➖"
                ),
                discord.SelectOption(label="Ingreso", value=MoneyDirection.INCOME.value, emoji="📥"),
                discord.SelectOption(label="Retiro", value=MoneyDirection.WITHDRAWAL.value, emoji="📤"),
            ],
            row=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(invisible=True)


class LogbookContinueButton(discord.ui.Button["LogbookSetupView"]):
    def __init__(self) -> None:
        super().__init__(
            label="Continuar",
            emoji="➡️",
            style=discord.ButtonStyle.primary,
            custom_id="logbook:v1:continue",
            row=2,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = cast(LogbookSetupView, self.view)
        await view.open_entry_modal(interaction)


class LogbookSetupView(discord.ui.View):
    def __init__(self, *, cog: LogbookCog, requester_id: int, guild_id: int, channel_id: int) -> None:
        super().__init__(timeout=MODAL_TIMEOUT_SECONDS, disable_on_timeout=True)
        self.cog = cog
        self.requester_id = requester_id
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.category = LogbookCategorySelect()
        self.money = LogbookMoneySelect()
        self.add_item(self.category)
        self.add_item(self.money)
        self.add_item(LogbookContinueButton())

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user is None or interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                embed=_generic_alert("Este selector pertenece a otra persona."),
                ephemeral=True,
            )
            return False
        return True

    async def open_entry_modal(self, interaction: discord.Interaction) -> None:
        if not await _validate_modal_context(
            interaction, self.cog, self.requester_id, self.guild_id, self.channel_id
        ):
            return
        if not self.category.values:
            await interaction.response.send_message(
                embed=_generic_alert("Selecciona una categoría antes de continuar."),
                ephemeral=True,
            )
            return
        raw_money = (self.money.values or ["none"])[0]
        category = LogbookCategory(self.category.values[0])
        money_direction = None if raw_money == "none" else MoneyDirection(raw_money)
        if category is LogbookCategory.FINANCE and money_direction is None:
            await interaction.response.send_message(
                embed=_generic_alert(
                    "Para registrar una novedad de Finanzas, selecciona si el movimiento "
                    "fue un ingreso o un retiro."
                ),
                ephemeral=True,
            )
            return
        await interaction.response.send_modal(
            LogbookEntryModal(
                cog=self.cog,
                requester_id=self.requester_id,
                guild_id=self.guild_id,
                channel_id=self.channel_id,
                category=category,
                money_direction=money_direction,
            )
        )
        self.stop()
        with suppress(discord.HTTPException):
            await interaction.delete_original_response()


class LogbookEntryModal(discord.ui.DesignerModal):
    def __init__(
        self,
        *,
        cog: LogbookCog,
        requester_id: int,
        guild_id: int,
        channel_id: int,
        category: LogbookCategory,
        money_direction: MoneyDirection | None,
    ) -> None:
        self.cog = cog
        self.requester_id = requester_id
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.category = category
        self.money_direction = money_direction
        presentation = CATEGORY_PRESENTATION[category]
        self.subtype = discord.ui.StringSelect(  # type: ignore[type-var]
            custom_id="logbook:v1:subtype",
            placeholder="Selecciona el tipo de novedad",
            options=[
                discord.SelectOption(label=label, value=key)
                for key, label in available_subtypes(category, money_direction)
            ],
        )
        self.subject = discord.ui.InputText(
            custom_id="logbook:v1:subject",
            placeholder=presentation.subject_label,
            max_length=200,
        )
        self.description = discord.ui.InputText(
            custom_id="logbook:v1:description",
            style=discord.InputTextStyle.long,
            placeholder="Describe lo ocurrido y los datos necesarios para comprenderlo.",
            max_length=2000,
        )
        self.amount = discord.ui.InputText(
            custom_id="logbook:v1:amount",
            placeholder="Ej.: 150000 o 150000,50",
            required=True,
            max_length=30,
        )
        self.files = discord.ui.FileUpload(
            custom_id="logbook:v1:files",
            min_values=0,
            max_values=10,
            required=False,
        )
        if money_direction is not None:
            super().__init__(
                discord.ui.Label("Tipo", self.subtype),
                discord.ui.Label("Concepto y contraparte", self.subject),
                discord.ui.Label("Descripción", self.description),
                discord.ui.Label("Monto", self.amount),
                discord.ui.Label("Adjuntos", self.files, description="Opcional · hasta 10 archivos"),
                title=f"{presentation.emoji} {presentation.label}"[:45],
                custom_id=f"logbook:v1:entry:{requester_id}",
                timeout=MODAL_TIMEOUT_SECONDS,
            )
        else:
            super().__init__(
                discord.ui.Label("Tipo", self.subtype),
                discord.ui.Label(presentation.subject_label, self.subject),
                discord.ui.Label("Descripción", self.description),
                discord.ui.Label("Adjuntos", self.files, description="Opcional · hasta 10 archivos"),
                title=f"{presentation.emoji} {presentation.label}"[:45],
                custom_id=f"logbook:v1:entry:{requester_id}",
                timeout=MODAL_TIMEOUT_SECONDS,
            )

    async def callback(self, interaction: discord.Interaction) -> None:
        attachments = tuple(cast(list[discord.Attachment], self.files.values or []))
        subtype = (self.subtype.values or ["other"])[0]
        await self.cog.publish(
            interaction,
            requester_id=self.requester_id,
            guild_id=self.guild_id,
            channel_id=self.channel_id,
            category=self.category,
            money_direction=self.money_direction,
            subtype=subtype,
            subject=self.subject.value or "",
            description=self.description.value or "",
            amount_text=(self.amount.value or "") if self.money_direction is not None else "",
            attachments=attachments,
        )


class LogbookGuideModal(discord.ui.DesignerModal):
    def __init__(
        self,
        *,
        cog: LogbookCog,
        requester_id: int,
        guild_id: int,
        channel_id: int,
    ) -> None:
        self.cog = cog
        self.requester_id = requester_id
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.publish = discord.ui.Checkbox(
            custom_id="logbook:v1:guide:publish",
            default=False,
        )
        super().__init__(
            discord.ui.Label("Publicar la guía en este canal", self.publish),
            title="Consultar guía de bitácora",
            custom_id=f"logbook:v1:guide:{requester_id}",
            timeout=MODAL_TIMEOUT_SECONDS,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog.show_guide(
            interaction,
            requester_id=self.requester_id,
            guild_id=self.guild_id,
            channel_id=self.channel_id,
            publish=bool(self.publish.value),
        )


async def _validate_modal_context(
    interaction: discord.Interaction,
    cog: LogbookCog,
    requester_id: int,
    guild_id: int,
    channel_id: int,
) -> bool:
    if (
        interaction.user is None
        or interaction.user.id != requester_id
        or interaction.guild is None
        or interaction.guild.id != guild_id
        or interaction.channel is None
        or interaction.channel.id != channel_id
    ):
        await interaction.response.send_message(
            embed=_generic_alert("Este formulario ya no es válido."), ephemeral=True
        )
        return False
    bundle = await cog.provider.cached(guild_id)
    ui = bundle.persona.profile.messages.interface.logbook
    denial = _availability_denial(bundle, channel_id, ui)
    if denial is not None:
        await interaction.response.send_message(embed=denial, ephemeral=True)
        return False
    return True


async def _validate_summary_context(
    interaction: discord.Interaction,
    cog: LogbookCog,
    requester_id: int,
    guild_id: int,
    channel_id: int,
) -> bool:
    if (
        not isinstance(interaction.user, discord.Member)
        or interaction.user.id != requester_id
        or interaction.guild is None
        or interaction.guild.id != guild_id
        or interaction.channel is None
        or interaction.channel.id != channel_id
    ):
        await interaction.response.send_message(
            embed=_generic_alert("Esta consulta ya no es válida."),
            ephemeral=True,
        )
        return False
    bundle = await cog.provider.cached(guild_id)
    logbook_ui = bundle.persona.profile.messages.interface.logbook
    report_ui = bundle.persona.profile.messages.interface.faction_reports
    feature = bundle.snapshot.features.get("logbook")
    if feature is None or not feature.enabled:
        await interaction.response.send_message(
            embed=_alert(logbook_ui.alert_title, logbook_ui.unavailable),
            ephemeral=True,
        )
        return False
    if not _member_has_any_role(
        interaction.user,
        bundle.settings.faction_report_role_ids,
    ):
        await interaction.response.send_message(
            embed=_alert(logbook_ui.alert_title, report_ui.unauthorized),
            ephemeral=True,
        )
        return False
    return True


def _availability_denial(bundle: object, channel_id: int, ui: PersonaLogbookMessages) -> discord.Embed | None:
    feature = bundle.snapshot.features.get("logbook")  # type: ignore[attr-defined]
    if feature is None or not feature.enabled:
        return _alert(ui.alert_title, ui.unavailable)
    if channel_id not in bundle.settings.logbook_channel_ids:  # type: ignore[attr-defined]
        return _alert(ui.alert_title, ui.wrong_channel)
    return None


def build_entry_embed(entry: LogbookEntry, *, attachment_count: int = 0) -> discord.Embed:
    presentation = CATEGORY_PRESENTATION[entry.category]
    embed = discord.Embed(
        title=f"{presentation.emoji} {entry.reference} · {subtype_label(entry.category, entry.subtype)}",
        description=entry.description,
        color=presentation.color,
        timestamp=entry.occurred_at,
    )
    embed.add_field(name="Categoría", value=presentation.label, inline=True)
    embed.add_field(name="Asunto", value=entry.subject, inline=True)
    if entry.money_direction is not None and entry.money_amount is not None:
        direction = "Ingreso" if entry.money_direction is MoneyDirection.INCOME else "Retiro"
        embed.add_field(
            name="💵 Movimiento",
            value=(
                f"**{direction}:** {_format_money(entry.money_amount)}\n"
                f"**Concepto:** {entry.money_detail or entry.subject}"
            ),
            inline=False,
        )
    if attachment_count:
        embed.add_field(name="📎 Respaldo", value=f"{attachment_count} adjunto(s)", inline=True)
    embed.add_field(name="Registrado por", value=f"<@{entry.author_user_id}>", inline=True)
    embed.set_footer(text=f"Bitácora · {entry.reference}")
    return embed


def build_summary_embed(summary: LogbookSummary) -> discord.Embed:
    category_label = (
        CATEGORY_PRESENTATION[summary.category_filter].label
        if summary.category_filter is not None
        else "Todos"
    )
    subtype_filter_label = (
        subtype_label(summary.category_filter, summary.subtype_filter)
        if summary.category_filter is not None and summary.subtype_filter is not None
        else "Todos"
    )
    embed = discord.Embed(
        title="📋 Resumen de bitácora",
        description=(
            f"`{_format_date(summary.date_from)} — {_format_date(summary.date_to)}` · "
            f"**{category_label} › {subtype_filter_label}**"
        ),
        color=discord.Color.dark_blue(),
    )
    embed.add_field(name="📝 Registros", value=str(summary.total_entries), inline=True)
    embed.add_field(name="📎 Adjuntos", value=str(summary.attachment_count), inline=True)
    embed.add_field(name="🏷️ Tipos", value=str(len(summary.subtypes)), inline=True)
    if not summary.total_entries:
        embed.add_field(
            name="Resultado",
            value="No hay registros publicados que coincidan con estos filtros.",
            inline=False,
        )
        return embed

    category_lines = [
        f"{CATEGORY_PRESENTATION[item.category].emoji} {CATEGORY_PRESENTATION[item.category].label} "
        f"**{item.count}**"
        for item in summary.categories
    ]
    embed.add_field(
        name="Distribución",
        value=" · ".join(category_lines),
        inline=False,
    )
    visible_subtypes = summary.subtypes[:SUMMARY_MAX_SUBTYPES]
    subtype_lines = [
        f"{CATEGORY_PRESENTATION[item.category].emoji} "
        f"{subtype_label(item.category, item.subtype)} **{item.count}**"
        for item in visible_subtypes
    ]
    hidden_subtypes = len(summary.subtypes) - len(visible_subtypes)
    if hidden_subtypes:
        subtype_lines.append(f"+{hidden_subtypes} tipos")
    embed.add_field(
        name="Tipos principales",
        value=" · ".join(subtype_lines),
        inline=False,
    )
    if summary.income_total or summary.withdrawal_total:
        embed.add_field(
            name="💵 Movimientos financieros",
            value=(
                f"📥 {_format_money(summary.income_total)} · "
                f"📤 {_format_money(summary.withdrawal_total)} · "
                f"**Balance {_format_signed_money(summary.balance)}**"
            ),
            inline=False,
        )
    return embed


def build_info_embed(ui: PersonaLogbookMessages) -> discord.Embed:
    embed = discord.Embed(
        title="📘 Cómo usar la bitácora",
        description=ui.info_intro,
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Registrar", value=ui.info_usage, inline=False)
    embed.add_field(name="Movimientos de dinero", value=ui.info_finance, inline=False)
    embed.add_field(name="Respaldos", value=ui.info_attachments, inline=False)
    embed.set_footer(text=ui.info_closing)
    return embed


def build_guide_embed(ui: PersonaLogbookMessages) -> discord.Embed:
    embed = discord.Embed(
        title="📚 Guía de tipos de bitácora",
        description=(
            "Elija en `/bitacora` la categoría que mejor represente la novedad y luego uno "
            "de sus tipos disponibles."
        ),
        color=discord.Color.blurple(),
    )
    for category, presentation in CATEGORY_PRESENTATION.items():
        if category is LogbookCategory.FINANCE:
            income = " · ".join(label for _, label in available_subtypes(category, MoneyDirection.INCOME))
            withdrawal = " · ".join(
                label for _, label in available_subtypes(category, MoneyDirection.WITHDRAWAL)
            )
            value = f"↳ **Ingreso:** {income}\n↳ **Retiro:** {withdrawal}"
        else:
            labels = " · ".join(label for _, label in available_subtypes(category, None))
            value = f"↳ {labels}"
        embed.add_field(
            name=f"{presentation.emoji} {presentation.label}",
            value=value,
            inline=True,
        )
    embed.set_footer(text=ui.info_closing)
    return embed


def parse_money_amount(value: str) -> Decimal:
    raw = value.strip().replace("$", "").replace(" ", "")
    if not raw or raw.startswith("-"):
        raise ValueError("Amount must be positive")
    if "," in raw and "." in raw:
        decimal_separator = "," if raw.rfind(",") > raw.rfind(".") else "."
        thousands_separator = "." if decimal_separator == "," else ","
        raw = raw.replace(thousands_separator, "").replace(decimal_separator, ".")
    elif "," in raw:
        head, tail = raw.rsplit(",", maxsplit=1)
        raw = f"{head.replace(',', '')}.{tail}" if len(tail) <= 2 else raw.replace(",", "")
    elif "." in raw:
        head, tail = raw.rsplit(".", maxsplit=1)
        raw = f"{head.replace('.', '')}.{tail}" if len(tail) <= 2 else raw.replace(".", "")
    try:
        amount = Decimal(raw).quantize(Decimal("0.01"))
    except InvalidOperation as error:
        raise ValueError("Invalid amount") from error
    if amount <= 0 or amount >= Decimal("100000000000000"):
        raise ValueError("Amount is outside the supported range")
    return amount


def _format_money(amount: Decimal) -> str:
    rendered = f"{amount:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
    return f"${rendered}"


def _format_signed_money(amount: Decimal) -> str:
    if amount > 0:
        return f"+{_format_money(amount)}"
    if amount < 0:
        return f"-{_format_money(abs(amount))}"
    return _format_money(amount)


def _format_date(value: date) -> str:
    return value.strftime("%d/%m/%Y")


def _member_has_any_role(member: discord.Member, role_ids: tuple[int, ...]) -> bool:
    allowed = set(role_ids)
    return bool(allowed and any(role.id in allowed for role in member.roles))


def _discord_files(files: tuple[LogbookFile, ...]) -> tuple[discord.File, ...]:
    used: set[str] = set()
    result: list[discord.File] = []
    for index, item in enumerate(files, start=1):
        filename = safe_filename(item.filename)
        if filename.casefold() in used:
            filename = f"{index}-{filename}"[:255]
        used.add(filename.casefold())
        result.append(discord.File(io.BytesIO(item.payload), filename=filename))
    return tuple(result)


def _alert(title: str, description: str) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=discord.Color.red())


def _generic_alert(description: str) -> discord.Embed:
    return _alert("⚠️ Bitácora no disponible", description)

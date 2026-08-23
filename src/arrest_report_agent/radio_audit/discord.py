"""Discord commands for deterministic radio queries and specialized AI audits."""

from __future__ import annotations

import asyncio
import csv
import io
import logging
from datetime import UTC, date, datetime
from math import ceil

import discord
from discord.ext import commands

from arrest_report_agent.application.date_ranges import DateRangePeriod, resolve_compact_date_range
from arrest_report_agent.config import BotPersona, RadioAuditOptions
from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.infrastructure.postgres import DatabaseConfigProvider
from arrest_report_agent.infrastructure.postgres.provider import GuildRuntimeBundle
from arrest_report_agent.knowledge import RadioCodeCatalog

from .agent import RadioAuditAgent
from .forms import (
    RadioResultVisibility,
    build_period_select,
    build_visibility_select,
    selected_period,
    selected_visibility,
)
from .manual import RadioManualCatalog
from .models import (
    ParsedRadioLog,
    RadioAuditFinding,
    RadioAuditResult,
    RadioAuditSeverity,
    RadioLogQueryResult,
    RadioLogScanTooLargeError,
)
from .service import RadioLogService

LOGGER = logging.getLogger(__name__)
MODAL_TIMEOUT_SECONDS = 300
VIEW_TIMEOUT_SECONDS = 300
RADIOS_PER_PAGE = 10
MAX_EMBED_RADIOS = 100
AUDIT_FINDINGS_PER_PAGE = 5


class RadioAuditCog(commands.Cog):
    """Expose the shared radio source through deterministic and AI workflows."""

    def __init__(
        self,
        provider: DatabaseConfigProvider,
        service: RadioLogService,
        radio_catalog: RadioCodeCatalog,
        manual: RadioManualCatalog,
        persona: BotPersona,
    ) -> None:
        self.provider = provider
        self.service = service
        self.radio_catalog = radio_catalog
        self.manual = manual
        self.default_persona = persona
        self._audit_locks: dict[int, asyncio.Lock] = {}

    @discord.slash_command(
        name="radios-faccion",
        description="Consulta registros recientes de radio de la facción",
    )
    async def faction_radios(self, ctx: discord.ApplicationContext) -> None:
        await self._open_modal(ctx, audit=False)

    @discord.slash_command(
        name="auditar-radio",
        description="Analiza el uso del radio de SCC con el manual",
    )
    async def audit_radio(self, ctx: discord.ApplicationContext) -> None:
        await self._open_modal(ctx, audit=True)

    async def _open_modal(self, ctx: discord.ApplicationContext, *, audit: bool) -> None:
        if ctx.guild is None or not isinstance(ctx.author, discord.Member):
            await ctx.respond(
                embed=_alert("Este comando solo funciona dentro de un servidor."), ephemeral=True
            )
            return
        try:
            bundle = await self.provider.cached(ctx.guild.id)
            options = bundle.settings.radio_audit_options
            if options is None:
                await ctx.respond(
                    embed=_alert("La consulta de radios no está habilitada aquí."), ephemeral=True
                )
                return
            role_ids = (
                bundle.settings.radio_audit_role_ids if audit else bundle.settings.operational_staff_role_ids
            )
            if not _member_has_any_role(ctx.author, role_ids):
                await ctx.respond(
                    embed=_unauthorized_embed(bundle.persona.profile, ctx.interaction.id),
                    ephemeral=True,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
                return
            modal: discord.ui.DesignerModal = (
                RadioAuditModal(self, ctx.author.id) if audit else RadioLogQueryModal(self, ctx.author.id)
            )
            await ctx.send_modal(modal)
        except Exception as error:
            log_event(
                LOGGER,
                "error",
                "radio_command_open_failed",
                guild_id=ctx.guild.id,
                user_id=ctx.author.id,
                command="auditar-radio" if audit else "radios-faccion",
                exc_info=error,
                **exception_fields(error),
            )
            await ctx.respond(embed=_alert("No pude abrir la consulta de radios."), ephemeral=True)

    async def run_query(
        self,
        interaction: discord.Interaction,
        *,
        requester_id: int,
        period: DateRangePeriod,
        raw_range: str,
        raw_slot: str,
        member_query: str,
        visibility: RadioResultVisibility,
    ) -> None:
        context = await self._validated_interaction(interaction, requester_id, audit=False)
        if context is None:
            return
        bundle, options = context
        try:
            date_from, date_to = resolve_compact_date_range(
                period,
                raw_range,
                max_days=options.query_max_range_days,
            )
            slot = _parse_slot(raw_slot)
        except ValueError:
            await interaction.response.send_message(
                embed=_alert(
                    "Use un slot positivo y fechas válidas. Para un rango personalizado escriba "
                    "`AAAA-MM-DD / AAAA-MM-DD`."
                ),
                ephemeral=True,
            )
            return
        private = visibility is RadioResultVisibility.PRIVATE
        await interaction.response.defer(ephemeral=private)
        try:
            result = await self.service.query(
                faction_id=options.faction_id,
                date_from=date_from,
                date_to=date_to,
                slot=slot,
                member_query=member_query,
                max_raw_records=options.query_max_records,
                newest_first=True,
            )
            view = RadioLogPaginationView(requester_id=requester_id, result=result)
            file = _radio_csv(result) if len(result.items) > MAX_EMBED_RADIOS else None
            kwargs: dict[str, object] = {
                "embed": view.current_embed,
                "ephemeral": private,
                "allowed_mentions": discord.AllowedMentions.none(),
            }
            if view.page_count > 1:
                kwargs["view"] = view
            if file is not None:
                kwargs["file"] = file
            await interaction.followup.send(**kwargs)  # type: ignore[arg-type]
            log_event(
                LOGGER,
                "info",
                "radio_logs_published",
                guild_id=interaction.guild_id,
                channel_id=interaction.channel_id,
                user_id=requester_id,
                visibility=visibility.value,
                date_from=date_from.isoformat(),
                date_to=date_to.isoformat(),
                slot=slot or "all",
                member_filter=bool(member_query.strip()),
                records=len(result.items),
                raw_records=result.scanned_count,
            )
        except RadioLogScanTooLargeError as error:
            await interaction.followup.send(
                embed=_alert(
                    f"El filtro contiene {error.total} registros y el máximo es {error.limit}. "
                    "Acote las fechas o el miembro."
                ),
                ephemeral=private,
            )
        except Exception as error:
            await self._publish_failure(interaction, error, private=private, event="radio_logs_failed")

    async def run_audit(
        self,
        interaction: discord.Interaction,
        *,
        requester_id: int,
        period: DateRangePeriod,
        raw_range: str,
        member_query: str,
        visibility: RadioResultVisibility,
    ) -> None:
        context = await self._validated_interaction(interaction, requester_id, audit=True)
        if context is None:
            return
        bundle, options = context
        try:
            date_from, date_to = resolve_compact_date_range(
                period,
                raw_range,
                max_days=options.audit_max_range_days,
            )
        except ValueError:
            await interaction.response.send_message(
                embed=_alert(
                    f"La auditoría admite hasta {options.audit_max_range_days} días. Para un "
                    "rango personalizado escriba `AAAA-MM-DD / AAAA-MM-DD`."
                ),
                ephemeral=True,
            )
            return
        private = visibility is RadioResultVisibility.PRIVATE
        lock = self._audit_locks.setdefault(interaction.guild_id or 0, asyncio.Lock())
        if lock.locked():
            await interaction.response.send_message(
                embed=_alert("Ya hay una auditoría radial en curso para este servidor."),
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=private)
        try:
            async with lock:
                sample = await self.service.query(
                    faction_id=options.faction_id,
                    date_from=date_from,
                    date_to=date_to,
                    slot=1,
                    member_query=member_query,
                    max_raw_records=options.query_max_records,
                    newest_first=False,
                )
                if not sample.items:
                    await interaction.followup.send(
                        embed=_empty_audit_embed(date_from, date_to),
                        ephemeral=private,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                    return
                if len(sample.items) > options.audit_max_records:
                    await interaction.followup.send(
                        embed=_alert(
                            f"La muestra contiene {len(sample.items)} radios del slot 1 y el máximo "
                            f"por auditoría es {options.audit_max_records}. Acote el filtro."
                        ),
                        ephemeral=private,
                    )
                    return
                agent = RadioAuditAgent(
                    bundle.settings,
                    bundle.model,
                    bundle.persona.profile,
                    self.radio_catalog,
                    self.manual,
                )
                result = await agent.audit(
                    sample,
                    guild_id=interaction.guild_id or 0,
                    requester_id=requester_id,
                    interaction_id=interaction.id,
                )
            view = RadioAuditPaginationView(
                requester_id=requester_id,
                result=result,
                sample=sample,
                date_from=date_from,
                date_to=date_to,
            )
            if view.page_count > 1:
                await interaction.followup.send(
                    embed=view.current_embed,
                    view=view,
                    ephemeral=private,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                await interaction.followup.send(
                    embed=view.current_embed,
                    ephemeral=private,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        except RadioLogScanTooLargeError as error:
            await interaction.followup.send(
                embed=_alert(
                    f"La consulta previa contiene {error.total} registros y el máximo es "
                    f"{error.limit}. Acote las fechas o el miembro."
                ),
                ephemeral=private,
            )
        except Exception as error:
            await self._publish_failure(interaction, error, private=private, event="radio_audit_failed")

    async def _validated_interaction(
        self,
        interaction: discord.Interaction,
        requester_id: int,
        *,
        audit: bool,
    ) -> tuple[GuildRuntimeBundle, RadioAuditOptions] | None:
        if (
            interaction.guild is None
            or interaction.channel is None
            or not isinstance(interaction.user, discord.Member)
            or interaction.user.id != requester_id
        ):
            await interaction.response.send_message(
                embed=_alert("La consulta ya no es válida."), ephemeral=True
            )
            return None
        bundle = await self.provider.cached(interaction.guild.id)
        options = bundle.settings.radio_audit_options
        if options is None:
            await interaction.response.send_message(
                embed=_alert("La consulta de radios no está habilitada aquí."), ephemeral=True
            )
            return None
        role_ids = (
            bundle.settings.radio_audit_role_ids if audit else bundle.settings.operational_staff_role_ids
        )
        if not _member_has_any_role(interaction.user, role_ids):
            await interaction.response.send_message(
                embed=_unauthorized_embed(bundle.persona.profile, interaction.id),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return None
        return bundle, options

    async def _publish_failure(
        self,
        interaction: discord.Interaction,
        error: Exception,
        *,
        private: bool,
        event: str,
    ) -> None:
        log_event(
            LOGGER,
            "error",
            event,
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            user_id=interaction.user.id,
            exc_info=error,
            **exception_fields(error),
        )
        await interaction.followup.send(
            embed=_alert("No pude revisar los registros de radio en este momento."),
            ephemeral=private,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class RadioLogQueryModal(discord.ui.DesignerModal):
    def __init__(self, cog: RadioAuditCog, requester_id: int) -> None:
        self.cog = cog
        self.requester_id = requester_id
        self.period = build_period_select(
            "radio_logs:v1:period",
            allowed_days=(1, 2, 7, 14, 30),
        )
        self.custom_range = discord.ui.InputText(
            custom_id="radio_logs:v1:range",
            placeholder="AAAA-MM-DD / AAAA-MM-DD",
            required=False,
            max_length=23,
        )
        self.slot = discord.ui.InputText(
            custom_id="radio_logs:v1:slot",
            value="1",
            placeholder="Vacío para todos",
            required=False,
            max_length=4,
        )
        self.member = discord.ui.InputText(
            custom_id="radio_logs:v1:member",
            placeholder="Nombre parcial · opcional",
            required=False,
            max_length=100,
        )
        self.visibility = build_visibility_select("radio_logs:v1:visibility")
        super().__init__(
            discord.ui.Label("Período", self.period),
            discord.ui.Label("Rango personalizado", self.custom_range),
            discord.ui.Label("Slot", self.slot),
            discord.ui.Label("Miembro", self.member),
            discord.ui.Label("Quién puede ver el resultado", self.visibility),
            title="Consultar radios de la facción",
            custom_id=f"radio_logs:v1:query:{requester_id}",
            timeout=MODAL_TIMEOUT_SECONDS,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog.run_query(
            interaction,
            requester_id=self.requester_id,
            period=selected_period(self.period),
            raw_range=self.custom_range.value or "",
            raw_slot=self.slot.value or "",
            member_query=self.member.value or "",
            visibility=selected_visibility(self.visibility),
        )


class RadioAuditModal(discord.ui.DesignerModal):
    def __init__(self, cog: RadioAuditCog, requester_id: int) -> None:
        self.cog = cog
        self.requester_id = requester_id
        self.period = build_period_select(
            "radio_audit:v1:period",
            allowed_days=(1, 2, 7),
        )
        self.custom_range = discord.ui.InputText(
            custom_id="radio_audit:v1:range",
            placeholder="AAAA-MM-DD / AAAA-MM-DD",
            required=False,
            max_length=23,
        )
        self.member = discord.ui.InputText(
            custom_id="radio_audit:v1:member",
            placeholder="Nombre parcial · opcional",
            required=False,
            max_length=100,
        )
        self.visibility = build_visibility_select("radio_audit:v1:visibility")
        super().__init__(
            discord.ui.Label("Período · máximo 7 días", self.period),
            discord.ui.Label("Rango personalizado", self.custom_range),
            discord.ui.Label("Miembro", self.member),
            discord.ui.Label("Quién puede ver el resultado", self.visibility),
            title="Auditar radio de SCC · slot 1",
            custom_id=f"radio_audit:v1:audit:{requester_id}",
            timeout=MODAL_TIMEOUT_SECONDS,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog.run_audit(
            interaction,
            requester_id=self.requester_id,
            period=selected_period(self.period),
            raw_range=self.custom_range.value or "",
            member_query=self.member.value or "",
            visibility=selected_visibility(self.visibility),
        )


class RadioLogPaginationView(discord.ui.View):
    def __init__(self, *, requester_id: int, result: RadioLogQueryResult) -> None:
        super().__init__(timeout=VIEW_TIMEOUT_SECONDS, disable_on_timeout=True)
        self.requester_id = requester_id
        self.result = result
        self.visible_items = result.items[:MAX_EMBED_RADIOS]
        self.page = 0
        self._sync()

    @property
    def page_count(self) -> int:
        return max(1, ceil(len(self.visible_items) / RADIOS_PER_PAGE))

    @property
    def current_embed(self) -> discord.Embed:
        start = self.page * RADIOS_PER_PAGE
        return build_radio_log_embed(
            self.result,
            self.visible_items[start : start + RADIOS_PER_PAGE],
            page=self.page + 1,
            page_count=self.page_count,
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.requester_id:
            return True
        await interaction.response.send_message("Esta consulta pertenece a otra persona.", ephemeral=True)
        return False

    @discord.ui.button(label="Anterior", style=discord.ButtonStyle.secondary, emoji="◀️")
    async def previous(self, _button: discord.ui.Button, interaction: discord.Interaction) -> None:
        self.page -= 1
        self._sync()
        await interaction.response.edit_message(embed=self.current_embed, view=self)

    @discord.ui.button(label="Siguiente", style=discord.ButtonStyle.secondary, emoji="▶️")
    async def next(self, _button: discord.ui.Button, interaction: discord.Interaction) -> None:
        self.page += 1
        self._sync()
        await interaction.response.edit_message(embed=self.current_embed, view=self)

    def _sync(self) -> None:
        self.previous.disabled = self.page <= 0
        self.next.disabled = self.page >= self.page_count - 1


class RadioAuditPaginationView(discord.ui.View):
    def __init__(
        self,
        *,
        requester_id: int,
        result: RadioAuditResult,
        sample: RadioLogQueryResult,
        date_from: date,
        date_to: date,
    ) -> None:
        super().__init__(timeout=VIEW_TIMEOUT_SECONDS, disable_on_timeout=True)
        self.requester_id = requester_id
        self.result = result
        self.sample = sample
        self.date_from = date_from
        self.date_to = date_to
        self.findings = tuple(finding for member in result.members for finding in member.findings)
        self.page = 0
        self._sync()

    @property
    def page_count(self) -> int:
        return max(1, ceil(len(self.findings) / AUDIT_FINDINGS_PER_PAGE))

    @property
    def current_embed(self) -> discord.Embed:
        start = self.page * AUDIT_FINDINGS_PER_PAGE
        return build_radio_audit_embed(
            self.result,
            self.sample,
            self.findings[start : start + AUDIT_FINDINGS_PER_PAGE],
            date_from=self.date_from,
            date_to=self.date_to,
            page=self.page + 1,
            page_count=self.page_count,
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.requester_id:
            return True
        await interaction.response.send_message("Esta auditoría pertenece a otra persona.", ephemeral=True)
        return False

    @discord.ui.button(label="Anterior", style=discord.ButtonStyle.secondary, emoji="◀️")
    async def previous(self, _button: discord.ui.Button, interaction: discord.Interaction) -> None:
        self.page -= 1
        self._sync()
        await interaction.response.edit_message(embed=self.current_embed, view=self)

    @discord.ui.button(label="Siguiente", style=discord.ButtonStyle.secondary, emoji="▶️")
    async def next(self, _button: discord.ui.Button, interaction: discord.Interaction) -> None:
        self.page += 1
        self._sync()
        await interaction.response.edit_message(embed=self.current_embed, view=self)

    def _sync(self) -> None:
        self.previous.disabled = self.page <= 0
        self.next.disabled = self.page >= self.page_count - 1


def build_radio_log_embed(
    result: RadioLogQueryResult,
    items: tuple[ParsedRadioLog, ...],
    *,
    page: int,
    page_count: int,
) -> discord.Embed:
    slot = str(result.slot) if result.slot is not None else "Todos"
    members = len({item.character_id for item in result.items})
    description = (
        f"📅 **Período UTC:** {result.date_from} → {result.date_to}\n"
        f"🎚️ **Slot:** {slot} · 👥 **Miembros:** {members} · 📨 **Registros:** {len(result.items)}"
    )
    if result.member_query:
        description += f"\n🔎 **Miembro:** {discord.utils.escape_markdown(result.member_query)}"
    embed = discord.Embed(
        title="📻 Registros de radio de la facción",
        description=description,
        color=discord.Color.blurple(),
        timestamp=datetime.now(UTC),
    )
    if not items:
        embed.add_field(
            name="Sin resultados", value="No hay radios que coincidan con el filtro.", inline=False
        )
    for item in items:
        timestamp = int(item.created_at.timestamp())
        header = f"<t:{timestamp}:t> · {item.character_name} · #{item.id}"
        prefix = "⚠️ Formato no reconocido · " if item.malformed else ""
        body = discord.utils.escape_markdown(_truncate(item.body, 350))
        embed.add_field(name=header, value=f"{prefix}{body}", inline=False)
    footer = f"Página {page} de {page_count}"
    if len(result.items) > MAX_EMBED_RADIOS:
        footer += f" · Mostrando 100 de {len(result.items)} · CSV adjunto con el resultado completo"
    if result.malformed_count:
        footer += f" · {result.malformed_count} registros con formato no reconocido"
    embed.set_footer(text=footer)
    return embed


def build_radio_audit_embed(
    result: RadioAuditResult,
    sample: RadioLogQueryResult,
    findings: tuple[RadioAuditFinding, ...],
    *,
    date_from: date,
    date_to: date,
    page: int,
    page_count: int,
) -> discord.Embed:
    color = discord.Color.green() if not result.members else discord.Color.orange()
    embed = discord.Embed(
        title="📡 Auditoría de radio · SCC / Slot 1",
        description=(
            f"{result.summary}\n\n"
            f"📅 **Período UTC:** {date_from.isoformat()} → {date_to.isoformat()}\n"
            f"📨 **Radios:** {result.total_radios} · 👥 **Miembros:** {result.total_members} · "
            f"✅ **Sin hallazgos:** {result.clean_members}\n"
            f"🟡 **Leves:** {result.light_count} · 🟠 **Moderadas:** {result.moderate_count} · "
            f"🔴 **Graves:** {result.severe_count}"
        ),
        color=color,
        timestamp=datetime.now(UTC),
    )
    by_id = {record.id: record for record in sample.items}
    if not findings:
        embed.add_field(
            name="✅ Sin observaciones",
            value="No se detectaron usos contrarios al manual en la muestra revisada.",
            inline=False,
        )
    for finding in findings:
        record = by_id[finding.radio_ids[0]]
        emoji = _severity_emoji(finding.severity)
        ids = ", ".join(f"#{identifier}" for identifier in finding.radio_ids)
        value = (
            f"<t:{int(record.created_at.timestamp())}:f> · {ids}\n"
            f"> {discord.utils.escape_markdown(_truncate(record.body, 180))}\n"
            f"{discord.utils.escape_markdown(_truncate(finding.explanation, 420))}\n"
            f"📖 {discord.utils.escape_markdown(finding.manual_reference)}"
        )
        embed.add_field(
            name=f"{emoji} {finding.member_name} · {finding.category}",
            value=value,
            inline=False,
        )
    embed.set_footer(
        text=(f"Página {page} de {page_count} · La gravedad describe la comunicación, no una sanción.")
    )
    return embed


def _radio_csv(result: RadioLogQueryResult) -> discord.File:
    text = io.StringIO(newline="")
    writer = csv.writer(text)
    writer.writerow(("id", "timestamp_utc", "channel", "slot", "character", "message"))
    for item in result.items:
        writer.writerow(
            (
                item.id,
                item.created_at.isoformat(),
                item.channel or "",
                item.slot or "",
                item.character_name,
                item.body,
            )
        )
    payload = io.BytesIO(text.getvalue().encode("utf-8-sig"))
    slot = f"slot-{result.slot}" if result.slot is not None else "todos-los-slots"
    return discord.File(payload, filename=f"radios-faccion-{result.date_from}-{slot}.csv")


def _empty_audit_embed(date_from: date, date_to: date) -> discord.Embed:
    return discord.Embed(
        title="📡 Auditoría de radio · SCC / Slot 1",
        description=(
            f"📅 **Período UTC:** {date_from.isoformat()} → {date_to.isoformat()}\n\n"
            "No hay registros del slot 1 que coincidan con el filtro. No se ejecutó la IA."
        ),
        color=discord.Color.green(),
    )


def _parse_slot(raw_value: str) -> int | None:
    normalized = raw_value.strip()
    if not normalized:
        return None
    value = int(normalized)
    if value <= 0:
        raise ValueError("slot must be positive")
    return value


def _member_has_any_role(member: discord.Member, role_ids: tuple[int, ...]) -> bool:
    allowed = set(role_ids)
    return bool(allowed and any(role.id in allowed for role in member.roles))


def _unauthorized_embed(persona: BotPersona, interaction_id: int) -> discord.Embed:
    messages = persona.messages.access
    intro = messages.unauthorized_user_intros[interaction_id % len(messages.unauthorized_user_intros)]
    description = messages.unauthorized_user.format(
        form_of_address=persona.identity.default_form_of_address,
        intro=intro,
    )
    return discord.Embed(title="🔒 Acceso restringido", description=description, color=discord.Color.red())


def _alert(description: str) -> discord.Embed:
    return discord.Embed(title="⚠️ Radios no disponibles", description=description, color=discord.Color.red())


def _truncate(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def _severity_emoji(severity: RadioAuditSeverity) -> str:
    return {
        RadioAuditSeverity.LIGHT: "🟡",
        RadioAuditSeverity.MODERATE: "🟠",
        RadioAuditSeverity.SEVERE: "🔴",
    }[severity]

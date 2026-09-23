"""Discord command and public persistent controls for warrant notifications."""

from __future__ import annotations

import logging
import re
from contextlib import suppress
from datetime import UTC, datetime
from enum import StrEnum
from typing import cast

import discord
from discord.ext import commands

from arrest_report_agent.config import WarrantNotificationOptions
from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.infrastructure.postgres import DatabaseConfigProvider

from .models import WarrantNotificationRecord, WarrantNotificationStatus
from .repository import WarrantNotificationRepository
from .service import WarrantDetail, WarrantLookupResult, WarrantLookupService

LOGGER = logging.getLogger(__name__)
MODAL_TIMEOUT_SECONDS = 300
DETAIL_BUTTON_CUSTOM_ID = "warrant-notification:v1:toggle"
SIGN_BUTTON_CUSTOM_ID = "warrant-notification:v1:sign"
REJECT_BUTTON_CUSTOM_ID = "warrant-notification:v1:reject"
ORDER_FIELD_NAME = "🔢 Número de orden"
STATUS_FIELD_NAME = "📌 Estado"
RESOLVER_FIELD_NAME = "👮 Resuelta por"
REASON_FIELD_NAME = "📝 Motivo del rechazo"
PENDING_NOTE = "⚠️ Firma o rechaza la orden en el servidor antes de marcarla aquí."
MAX_DESCRIPTION_CHARS = 3_900
MAX_FIELD_CHARS = 1_024
_ORDER_NUMBER = re.compile(r"\d+")


class WarrantType(StrEnum):
    ARREST = "arrest"
    SEARCH = "search"
    BOTH = "both"

    @property
    def label(self) -> str:
        return {
            self.ARREST: "Arresto",
            self.SEARCH: "Allanamiento",
            self.BOTH: "Arresto y allanamiento",
        }[self]

    @property
    def emoji(self) -> str:
        return {
            self.ARREST: "🚓",
            self.SEARCH: "🔍",
            self.BOTH: "⚖️",
        }[self]


class WarrantNotificationCog(commands.Cog):
    """Publish compact notifications without calling SARP until requested."""

    def __init__(
        self,
        provider: DatabaseConfigProvider,
        lookup: WarrantLookupService,
        repository: WarrantNotificationRepository,
    ) -> None:
        self.provider = provider
        self.lookup = lookup
        self.repository = repository
        self.notify_warrant.description = "Notifica una orden a los encargados"
        self.warrant_info.description = "Explica cómo notificar órdenes"

    @discord.slash_command(
        name="notificarorden",
        description="Notifica una orden a los encargados",
    )
    async def notify_warrant(self, ctx: discord.ApplicationContext) -> None:
        member, options = await self._authorized_context(ctx)
        if member is None or options is None:
            return
        try:
            await _destination_channel(member.guild, options)
            await _result_channel(member.guild, options)
        except Exception as error:
            _log_command_error("warrant_destination_unavailable", error, ctx)
            await ctx.respond(embed=_command_failure(), ephemeral=True)
            return
        await ctx.send_modal(WarrantNotificationModal(self, member))

    @discord.slash_command(
        name="info-ordenes",
        description="Explica cómo notificar órdenes",
    )
    async def warrant_info(self, ctx: discord.ApplicationContext) -> None:
        member, options = await self._authorized_context(ctx)
        if member is None or options is None:
            return
        await ctx.respond(embed=build_warrant_info_embed(), ephemeral=False)

    async def _authorized_context(
        self,
        ctx: discord.ApplicationContext,
    ) -> tuple[discord.Member | None, WarrantNotificationOptions | None]:
        if ctx.guild is None or not isinstance(ctx.author, discord.Member):
            await ctx.respond(
                embed=_alert(
                    "⚠️ Órdenes no disponibles",
                    "Este comando solo funciona dentro de un servidor.",
                ),
                ephemeral=True,
            )
            return None, None
        try:
            bundle = await self.provider.cached(ctx.guild.id)
        except Exception as error:
            _log_command_error("warrant_configuration_failed", error, ctx)
            await ctx.respond(embed=_command_failure(), ephemeral=True)
            return None, None
        options = bundle.settings.warrant_notification_options
        if options is None:
            await ctx.respond(
                embed=_alert(
                    "⚠️ Órdenes no disponibles",
                    "La notificación de órdenes no está habilitada en este servidor.",
                ),
                ephemeral=True,
            )
            return None, None
        if not _has_any_role(ctx.author, bundle.settings.operational_staff_role_ids):
            await ctx.respond(
                embed=_alert(
                    "🚫 Tú no puedes usar esto",
                    "Este comando es exclusivamente para miembros de la facción.",
                ),
                ephemeral=True,
            )
            return None, None
        return ctx.author, options

    async def publish(
        self,
        interaction: discord.Interaction,
        *,
        member: discord.Member,
        report_url: str,
        warrant_id: int,
        warrant_type: WarrantType,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        record: WarrantNotificationRecord | None = None
        message: discord.Message | None = None
        published = False
        try:
            bundle = await self.provider.cached(member.guild.id)
            options = bundle.settings.warrant_notification_options
            if options is None or not _has_any_role(
                member,
                bundle.settings.operational_staff_role_ids,
            ):
                raise PermissionError("warrant notifications are unavailable for this member")
            channel = await _destination_channel(member.guild, options)
            await _result_channel(member.guild, options)
            roles = _notification_roles(member.guild, options)
            if not roles:
                raise LookupError("no configured warrant notification roles are available")
            record, created = await self.repository.create_publishing(
                guild_id=member.guild.id,
                interaction_id=interaction.id,
                channel_id=channel.id,
                requester_user_id=member.id,
                report_url=report_url,
                warrant_id=warrant_id,
                warrant_type=warrant_type.value,
            )
            if not created:
                await interaction.followup.send(
                    embed=(
                        _command_success(record.warrant_id)
                        if record.message_id is not None
                        else _alert("⏳ Notificación en curso", "La solicitud ya se está procesando.")
                    ),
                    ephemeral=True,
                )
                return
            message = await channel.send(
                content=" ".join(role.mention for role in roles),
                embed=build_warrant_notification_embed(
                    member,
                    report_url=report_url,
                    warrant_id=warrant_id,
                    warrant_type=warrant_type,
                ),
                view=WarrantNotificationView(self.lookup, self.provider, self.repository, show_actions=True),
                allowed_mentions=discord.AllowedMentions(
                    everyone=False,
                    users=False,
                    roles=list(roles),
                    replied_user=False,
                ),
            )
            await self.repository.publish(record.id, message.id)
            published = True
            with suppress(Exception):
                await interaction.followup.send(
                    embed=_command_success(warrant_id),
                    ephemeral=True,
                )
            log_event(
                LOGGER,
                "info",
                "warrant_notification_published",
                guild_id=member.guild.id,
                channel_id=channel.id,
                message_id=message.id,
                user_id=member.id,
                warrant_id=warrant_id,
                warrant_type=warrant_type.value,
            )
        except Exception as error:
            if message is not None and not published:
                with suppress(Exception):
                    await message.delete()
            if record is not None and not published:
                with suppress(Exception):
                    await self.repository.mark_failed(record.id)
            log_event(
                LOGGER,
                "error",
                "warrant_notification_publish_failed",
                exc_info=error,
                guild_id=member.guild.id,
                user_id=member.id,
                warrant_id=warrant_id,
                **exception_fields(error),
            )
            await interaction.followup.send(embed=_command_failure(), ephemeral=True)


class WarrantNotificationModal(discord.ui.DesignerModal):
    def __init__(self, cog: WarrantNotificationCog, member: discord.Member) -> None:
        self.cog = cog
        self.member_id = member.id
        self.report_url = discord.ui.InputText(
            custom_id="warrant-notification:v1:create:report-url",
            placeholder="Pega aquí la URL del reporte de incidente.",
            min_length=1,
            max_length=1_000,
        )
        self.warrant_number = discord.ui.InputText(
            custom_id="warrant-notification:v1:create:number",
            placeholder="Ejemplo: 1324",
            min_length=1,
            max_length=30,
        )
        self.warrant_type = discord.ui.StringSelect(  # type: ignore[type-var]
            custom_id="warrant-notification:v1:create:type",
            placeholder="Selecciona el tipo de orden",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label=item.label, value=item.value, emoji=item.emoji)
                for item in WarrantType
            ],
        )
        super().__init__(
            discord.ui.Label("Reporte de incidente", self.report_url),
            discord.ui.Label("Número de orden", self.warrant_number),
            discord.ui.Label("Tipo de orden", self.warrant_type),
            title="Notificar una orden",
            custom_id=f"warrant-notification:v1:create:{member.id}",
            timeout=MODAL_TIMEOUT_SECONDS,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        member = interaction.user
        if not isinstance(member, discord.Member) or member.id != self.member_id:
            await interaction.response.send_message(embed=_command_failure(), ephemeral=True)
            return
        warrant_id = _parse_warrant_id(self.warrant_number.value or "")
        if warrant_id is None:
            await interaction.response.send_message(
                embed=_alert(
                    "⚠️ Número de orden inválido",
                    "Escribe únicamente el número de la orden, por ejemplo `1324`.",
                ),
                ephemeral=True,
            )
            return
        selected_types = self.warrant_type.values or []
        if not selected_types:
            await interaction.response.send_message(embed=_command_failure(), ephemeral=True)
            return
        await self.cog.publish(
            interaction,
            member=member,
            report_url=(self.report_url.value or "").strip(),
            warrant_id=warrant_id,
            warrant_type=WarrantType(selected_types[0]),
        )


class WarrantNotificationView(discord.ui.View):
    def __init__(
        self,
        lookup: WarrantLookupService,
        provider: DatabaseConfigProvider | None = None,
        repository: WarrantNotificationRepository | None = None,
        *,
        mode: str = "collapsed",
        disabled: bool = False,
        show_actions: bool | None = None,
    ) -> None:
        super().__init__(timeout=None)
        self.lookup = lookup
        self.provider = provider
        self.repository = repository
        if show_actions is None:
            show_actions = provider is not None and repository is not None
        button = cast("discord.ui.Button[WarrantNotificationView]", self.children[0])
        if mode == "expanded":
            button.label = "Ocultar detalle"
            button.emoji = "📋"
            button.style = discord.ButtonStyle.secondary
        elif mode == "retry":
            button.label = "Reintentar consulta"
            button.emoji = "🔄"
            button.style = discord.ButtonStyle.primary
        button.disabled = disabled
        if not show_actions:
            for item in tuple(self.children):
                if getattr(item, "custom_id", None) in {
                    SIGN_BUTTON_CUSTOM_ID,
                    REJECT_BUTTON_CUSTOM_ID,
                }:
                    self.remove_item(item)  # type: ignore[arg-type]  # Pycord's item typing is too narrow.
        elif disabled:
            for item in self.children:
                if isinstance(item, discord.ui.Button):
                    item.disabled = True

    @discord.ui.button(
        label="Ver detalle",
        emoji="📋",
        style=discord.ButtonStyle.primary,
        custom_id=DETAIL_BUTTON_CUSTOM_ID,
    )
    async def detail_button(
        self,
        _button: discord.ui.Button[WarrantNotificationView],
        interaction: discord.Interaction,
    ) -> None:
        message = interaction.message
        if message is None:
            await interaction.response.send_message(embed=_command_failure(), ephemeral=True)
            return
        await interaction.response.defer()
        record = await self._record_for_message(interaction, message)
        original = _record_embed(message.embeds[0], record) if message.embeds else None
        warrant_id = _warrant_id_from_embed(original)
        if original is None or warrant_id is None:
            await message.edit(
                embeds=[original, _lookup_alert(0, "temporarily_unavailable")]
                if original is not None
                else [_lookup_alert(0, "temporarily_unavailable")],
                view=self._view(record, mode="retry"),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        if len(message.embeds) > 1 and (message.embeds[1].title or "").startswith("📋 Detalle"):
            await message.edit(
                embeds=[original],
                view=self._view(record),
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        await message.edit(
            view=self._view(
                record,
                mode="retry" if len(message.embeds) > 1 else "collapsed",
                disabled=True,
            ),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        result = await self.lookup.get(warrant_id)
        detail_embed, mode = _lookup_result_embed(warrant_id, result)
        record = await self._record_for_message(interaction, message)
        original = _record_embed(original, record)
        if record is not None:
            detail_embed.color = _status_color(record.status)
        await message.edit(
            embeds=[original, detail_embed],
            view=self._view(record, mode=mode),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        log_event(
            LOGGER,
            "info" if result.detail is not None else "warning",
            "warrant_notification_detail_rendered",
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
            message_id=message.id,
            user_id=getattr(interaction.user, "id", None),
            warrant_id=warrant_id,
            outcome="found" if result.detail is not None else result.error,
        )

    @discord.ui.button(
        label="Firmar",
        emoji="✍️",
        style=discord.ButtonStyle.success,
        custom_id=SIGN_BUTTON_CUSTOM_ID,
    )
    async def sign_button(
        self,
        _button: discord.ui.Button[WarrantNotificationView],
        interaction: discord.Interaction,
    ) -> None:
        message = interaction.message
        if message is None:
            await interaction.response.send_message(embed=_command_failure(), ephemeral=True)
            return
        await interaction.response.defer()
        await self._resolve(
            interaction,
            message,
            status=WarrantNotificationStatus.SIGNED,
        )

    @discord.ui.button(
        label="Rechazar",
        emoji="❌",
        style=discord.ButtonStyle.danger,
        custom_id=REJECT_BUTTON_CUSTOM_ID,
    )
    async def reject_button(
        self,
        _button: discord.ui.Button[WarrantNotificationView],
        interaction: discord.Interaction,
    ) -> None:
        message = interaction.message
        if message is None:
            await interaction.response.send_message(embed=_command_failure(), ephemeral=True)
            return
        try:
            record, _options = await self._resolution_context(interaction, message)
        except (LookupError, PermissionError) as error:
            await interaction.response.send_message(embed=_resolution_error(error), ephemeral=True)
            return
        if record.status is not WarrantNotificationStatus.PENDING:
            await interaction.response.send_message(embed=_already_resolved(record), ephemeral=True)
            return
        actor = interaction.user
        if not isinstance(actor, discord.Member):
            await interaction.response.send_message(embed=_command_failure(), ephemeral=True)
            return
        await interaction.response.send_modal(WarrantRejectionModal(self, message, actor.id))

    async def _resolve(
        self,
        interaction: discord.Interaction,
        message: discord.Message,
        *,
        status: WarrantNotificationStatus,
        reason: str | None = None,
    ) -> None:
        try:
            record, options = await self._resolution_context(interaction, message)
            if self.repository is None:
                raise LookupError("warrant notification repository is unavailable")
            actor = interaction.user
            if not isinstance(actor, discord.Member):
                raise LookupError("warrant notification member is unavailable")
            if record.status is WarrantNotificationStatus.PENDING:
                record, changed = await self.repository.resolve(
                    record.id,
                    status=status,
                    resolver_user_id=actor.id,
                    rejection_reason=reason,
                )
            else:
                changed = False
            if not changed:
                if record.status.resolved:
                    await self._refresh_message(message, record)
                    alert = _already_resolved(record)
                else:
                    alert = _alert("⏳ Inténtalo de nuevo", "La orden sigue pendiente.")
                await interaction.followup.send(embed=alert, ephemeral=True)
                return
            try:
                guild = interaction.guild
                if guild is None:
                    raise LookupError("warrant notification guild is unavailable")
                result_channel = await _result_channel(guild, options)
                result_message = await result_channel.send(
                    content=f"<@{record.requester_user_id}>",
                    embed=build_warrant_resolution_embed(record),
                    allowed_mentions=discord.AllowedMentions(
                        everyone=False,
                        users=[discord.Object(id=record.requester_user_id)],
                        roles=False,
                        replied_user=False,
                    ),
                )
            except Exception as error:
                restored = False
                try:
                    restored = await self.repository.restore_pending(
                        record.id,
                        expected_status=status,
                        resolver_user_id=actor.id,
                    )
                except Exception as restore_error:
                    log_event(
                        LOGGER,
                        "error",
                        "warrant_resolution_restore_failed",
                        exc_info=restore_error,
                        guild_id=record.guild_id,
                        message_id=record.message_id,
                        **exception_fields(restore_error),
                    )
                log_event(
                    LOGGER,
                    "error",
                    "warrant_resolution_notification_failed",
                    exc_info=error,
                    guild_id=record.guild_id,
                    message_id=record.message_id,
                    **exception_fields(error),
                )
                await interaction.followup.send(
                    embed=_alert(
                        "⚠️ No pude avisar al solicitante",
                        (
                            "La notificación sigue pendiente; puedes volver a intentarlo."
                            if restored
                            else "No pude recuperar el estado. Contacta a Command Staff."
                        ),
                    ),
                    ephemeral=True,
                )
                return
            try:
                await self.repository.mark_result_sent(record.id, result_message.id)
            except Exception as error:
                log_event(
                    LOGGER,
                    "error",
                    "warrant_resolution_result_tracking_failed",
                    exc_info=error,
                    guild_id=record.guild_id,
                    message_id=record.message_id,
                    result_message_id=result_message.id,
                    **exception_fields(error),
                )
            card_error: Exception | None = None
            try:
                await self._refresh_message(message, record)
            except Exception as error:
                card_error = error
                log_event(
                    LOGGER,
                    "error",
                    "warrant_resolution_card_update_failed",
                    exc_info=error,
                    guild_id=record.guild_id,
                    message_id=record.message_id,
                    **exception_fields(error),
                )
            if card_error is not None:
                await interaction.followup.send(
                    embed=_alert(
                        "⚠️ La tarjeta no se actualizó",
                        "Avisé al solicitante, pero no pude actualizar el estado visual de la orden.",
                    ),
                    ephemeral=True,
                )
            log_event(
                LOGGER,
                "info",
                "warrant_notification_resolved",
                guild_id=record.guild_id,
                message_id=record.message_id,
                warrant_id=record.warrant_id,
                status=record.status.value,
                resolver_user_id=record.resolved_by_user_id,
                result_message_id=result_message.id,
            )
        except (LookupError, PermissionError) as error:
            await interaction.followup.send(embed=_resolution_error(error), ephemeral=True)
        except Exception as error:
            log_event(
                LOGGER,
                "error",
                "warrant_resolution_failed",
                exc_info=error,
                guild_id=interaction.guild_id,
                message_id=message.id,
                **exception_fields(error),
            )
            await interaction.followup.send(embed=_command_failure(), ephemeral=True)

    async def _resolution_context(
        self,
        interaction: discord.Interaction,
        message: discord.Message,
    ) -> tuple[WarrantNotificationRecord, WarrantNotificationOptions]:
        if (
            self.provider is None
            or self.repository is None
            or interaction.guild is None
            or interaction.channel_id is None
            or not isinstance(interaction.user, discord.Member)
        ):
            raise LookupError("warrant notification context is unavailable")
        bundle = await self.provider.cached(interaction.guild.id)
        options = bundle.settings.warrant_notification_options
        if options is None:
            raise LookupError("warrant notifications are disabled")
        if not _has_any_role(interaction.user, options.notification_role_ids):
            raise PermissionError("Only Command and Executive Staff can resolve orders")
        record = await self.repository.get_by_message(
            interaction.guild.id, interaction.channel_id, message.id
        )
        if record is None:
            raise LookupError("warrant notification was not found")
        return record, options

    async def _record_for_message(
        self,
        interaction: discord.Interaction,
        message: discord.Message,
    ) -> WarrantNotificationRecord | None:
        if self.repository is None or interaction.guild_id is None or interaction.channel_id is None:
            return None
        try:
            return await self.repository.get_by_message(
                interaction.guild_id, interaction.channel_id, message.id
            )
        except Exception as error:
            log_event(
                LOGGER,
                "warning",
                "warrant_notification_detail_state_unavailable",
                exc_info=error,
                guild_id=interaction.guild_id,
                message_id=message.id,
                **exception_fields(error),
            )
            return None

    def _view(
        self,
        record: WarrantNotificationRecord | None,
        *,
        mode: str = "collapsed",
        disabled: bool = False,
    ) -> WarrantNotificationView:
        return WarrantNotificationView(
            self.lookup,
            self.provider,
            self.repository,
            mode=mode,
            disabled=disabled,
            show_actions=record is not None and record.status is WarrantNotificationStatus.PENDING,
        )

    async def _refresh_message(
        self,
        message: discord.Message,
        record: WarrantNotificationRecord,
    ) -> None:
        current = message
        if getattr(message, "channel", None) is not None:
            current = await message.channel.fetch_message(message.id)
        if not current.embeds:
            raise LookupError("warrant notification card is missing")
        original = _record_embed(current.embeds[0], record)
        embeds = [original]
        mode = "collapsed"
        if len(current.embeds) > 1:
            detail = discord.Embed.from_dict(current.embeds[1].to_dict())
            detail.color = _status_color(record.status)
            embeds.append(detail)
            mode = "expanded" if (detail.title or "").startswith("📋 Detalle") else "retry"
        await current.edit(
            embeds=embeds,
            view=self._view(record, mode=mode),
            allowed_mentions=discord.AllowedMentions.none(),
        )


class WarrantRejectionModal(discord.ui.DesignerModal):
    def __init__(
        self,
        view: WarrantNotificationView,
        message: discord.Message,
        actor_user_id: int,
    ) -> None:
        self.view = view
        self.message = message
        self.actor_user_id = actor_user_id
        self.reason = discord.ui.InputText(
            custom_id="warrant-notification:v1:reject:reason",
            style=discord.InputTextStyle.long,
            placeholder="Explica brevemente el motivo, si corresponde.",
            required=False,
            max_length=500,
        )
        super().__init__(
            discord.ui.Label("Justificación (opcional)", self.reason),
            title="Rechazar orden",
            custom_id=f"warrant-notification:v1:reject-modal:{message.id}",
            timeout=MODAL_TIMEOUT_SECONDS,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user is None or interaction.user.id != self.actor_user_id:
            await interaction.response.send_message(embed=_command_failure(), ephemeral=True)
            return
        await interaction.response.defer()
        await self.view._resolve(
            interaction,
            self.message,
            status=WarrantNotificationStatus.REJECTED,
            reason=self.reason.value,
        )


def build_warrant_notification_embed(
    member: discord.Member,
    *,
    report_url: str,
    warrant_id: int,
    warrant_type: WarrantType,
) -> discord.Embed:
    embed = discord.Embed(
        title=f"{warrant_type.emoji} Notificación de orden · {warrant_type.label}",
        description=PENDING_NOTE,
        color=_status_color(WarrantNotificationStatus.PENDING),
    )
    embed.set_author(name=member.display_name, icon_url=member.display_avatar.url)
    embed.add_field(name=ORDER_FIELD_NAME, value=f"N.º {warrant_id}", inline=True)
    embed.add_field(name=STATUS_FIELD_NAME, value="🟡 Pendiente", inline=True)
    embed.add_field(
        name="📄 Reporte de incidente",
        value=f"[Abrir reporte de incidente]({report_url})",
        inline=False,
    )
    return embed


def build_warrant_resolution_embed(record: WarrantNotificationRecord) -> discord.Embed:
    label = "Firmada" if record.status is WarrantNotificationStatus.SIGNED else "Rechazada"
    emoji = "✅" if record.status is WarrantNotificationStatus.SIGNED else "❌"
    embed = discord.Embed(
        title=f"{emoji} Orden {label.lower()} · N.º {record.warrant_id}",
        description=(
            f"**Solicitante:** <@{record.requester_user_id}>\n"
            f"**Resuelta por:** <@{record.resolved_by_user_id}>"
        ),
        color=_status_color(record.status),
    )
    if record.rejection_reason:
        embed.add_field(
            name=REASON_FIELD_NAME,
            value=_safe_display_text(record.rejection_reason),
            inline=False,
        )
    if record.jump_url:
        embed.add_field(
            name="📋 Notificación original",
            value=f"[Ver orden]({record.jump_url})",
            inline=False,
        )
    return embed


def _record_embed(embed: discord.Embed, record: WarrantNotificationRecord | None) -> discord.Embed:
    if record is None:
        return embed
    updated = discord.Embed.from_dict(embed.to_dict())
    updated.color = _status_color(record.status)
    updated.description = PENDING_NOTE if record.status is WarrantNotificationStatus.PENDING else None
    status_value = {
        WarrantNotificationStatus.PUBLISHING: "🟡 Pendiente",
        WarrantNotificationStatus.PENDING: "🟡 Pendiente",
        WarrantNotificationStatus.SIGNED: "✅ Firmada",
        WarrantNotificationStatus.REJECTED: "❌ Rechazada",
        WarrantNotificationStatus.FAILED: "⚠️ No publicada",
    }[record.status]
    _set_or_add_field(updated, STATUS_FIELD_NAME, status_value, inline=True)
    if record.resolved_by_user_id:
        _set_or_add_field(updated, RESOLVER_FIELD_NAME, f"<@{record.resolved_by_user_id}>", inline=True)
    if record.rejection_reason:
        _set_or_add_field(
            updated,
            REASON_FIELD_NAME,
            _safe_display_text(record.rejection_reason),
            inline=False,
        )
    return updated


def _set_or_add_field(embed: discord.Embed, name: str, value: str, *, inline: bool) -> None:
    for index, field in enumerate(embed.fields):
        if field.name == name:
            embed.set_field_at(index, name=name, value=value, inline=inline)
            return
    embed.add_field(name=name, value=value, inline=inline)


def _status_color(status: WarrantNotificationStatus) -> discord.Color:
    return {
        WarrantNotificationStatus.PUBLISHING: discord.Color.gold(),
        WarrantNotificationStatus.PENDING: discord.Color.gold(),
        WarrantNotificationStatus.SIGNED: discord.Color.green(),
        WarrantNotificationStatus.REJECTED: discord.Color.red(),
        WarrantNotificationStatus.FAILED: discord.Color.dark_grey(),
    }[status]


def _already_resolved(record: WarrantNotificationRecord) -> discord.Embed:
    label = "firmada" if record.status is WarrantNotificationStatus.SIGNED else "rechazada"
    return _alert(
        "📌 Orden ya resuelta",
        f"Esta notificación ya fue marcada como {label}. No se enviará otro aviso.",
    )


def _resolution_error(error: Exception) -> discord.Embed:
    if isinstance(error, PermissionError):
        return _alert(
            "🚫 No puedes resolver esta orden",
            "Solo Command y Executive Staff pueden firmar o rechazar aquí.",
        )
    return _alert(
        "⚠️ Notificación no disponible",
        "No pude identificar esta notificación. Pide que la publiquen nuevamente.",
    )


def build_warrant_detail_embed(detail: WarrantDetail) -> discord.Embed:
    embed = discord.Embed(
        title=f"📋 Detalle de la orden N.º {detail.warrant_id}",
        color=_detail_color(detail),
    )
    embed.add_field(
        name="👤 Persona requerida",
        value=f"└ {_safe_display_text(detail.suspect_name)}",
        inline=False,
    )
    embed.add_field(
        name="🚓 Tipo y estado",
        value=(
            f"├ Tipo: {_warrant_type_label(detail.types)}\n"
            f"├ Estado: {_status_label(detail)}\n"
            f"└ Riesgo: {_risk_label(detail.risk)}"
        ),
        inline=False,
    )
    process_lines = _process_lines(detail)
    if process_lines:
        embed.add_field(
            name="👮 Tramitación",
            value=_tree_lines(process_lines),
            inline=False,
        )
    narrative = discord.utils.escape_mentions(
        detail.description or "No hay una descripción registrada para esta orden."
    )
    for index, chunk in enumerate(_field_chunks(narrative, max_total=MAX_DESCRIPTION_CHARS)):
        embed.add_field(
            name="📝 Fundamento de la orden" if index == 0 else "📝 Fundamento · continuación",
            value=chunk,
            inline=False,
        )
    embed.add_field(
        name=f"⚖️ Cargos imputados · {len(detail.charges)}",
        value=_charge_lines(detail),
        inline=False,
    )
    return embed


def build_warrant_info_embed() -> discord.Embed:
    return discord.Embed(
        title="📋 Cómo notificar una orden",
        description=(
            "Usa `/notificarorden` cuando un reporte de incidente derive en una orden "
            "que los encargados deban conocer.\n\n"
            "**Necesitarás**\n"
            "└ 📄 URL del reporte de incidente\n"
            "└ 🔢 Número de orden\n"
            "└ 🚓 Arresto, 🔍 Allanamiento o ⚖️ Ambos\n\n"
            "La notificación llegará automáticamente a los encargados. "
            "Después de resolverla en el servidor, Command o Executive marcarán "
            "el resultado aquí y recibirás un aviso.\n\n"
            "*Sí, debes escribir el reporte antes de venir a tocar el timbre.*"
        ),
        color=discord.Color.blurple(),
    )


async def _destination_channel(
    guild: discord.Guild,
    options: WarrantNotificationOptions,
) -> discord.TextChannel:
    return await _text_channel(guild, options.channel_id)


async def _result_channel(
    guild: discord.Guild,
    options: WarrantNotificationOptions,
) -> discord.TextChannel:
    return await _text_channel(guild, options.resolution_channel_id)


async def _text_channel(guild: discord.Guild, channel_id: int) -> discord.TextChannel:
    resolved: object | None = guild.get_channel(channel_id)
    if resolved is None:
        resolved = await guild.fetch_channel(channel_id)
    if not isinstance(resolved, discord.TextChannel):
        raise LookupError("warrant notification destination is not a text channel")
    bot_member = guild.me
    if bot_member is None:
        raise LookupError("bot guild member is unavailable")
    permissions = resolved.permissions_for(bot_member)
    if not permissions.send_messages or not permissions.embed_links:
        raise PermissionError("bot cannot publish warrant notifications")
    return resolved


def _notification_roles(
    guild: discord.Guild,
    options: WarrantNotificationOptions,
) -> tuple[discord.Role, ...]:
    return tuple(
        role for role_id in options.notification_role_ids if (role := guild.get_role(role_id)) is not None
    )


def _warrant_id_from_embed(embed: discord.Embed | None) -> int | None:
    if embed is None:
        return None
    for field in embed.fields:
        if field.name == ORDER_FIELD_NAME:
            return _parse_warrant_id(str(field.value))
    return None


def _parse_warrant_id(value: str) -> int | None:
    match = _ORDER_NUMBER.search(value)
    if match is None:
        return None
    parsed = int(match.group())
    return parsed if parsed > 0 else None


def _lookup_result_embed(
    warrant_id: int,
    result: WarrantLookupResult,
) -> tuple[discord.Embed, str]:
    if result.detail is not None:
        return build_warrant_detail_embed(result.detail), "expanded"
    return _lookup_alert(warrant_id, result.error), "retry"


def _lookup_alert(warrant_id: int, error: str | None) -> discord.Embed:
    if error == "not_found":
        return _alert(
            "⚠️ Orden no encontrada",
            (
                f"No encontré la orden N.º {warrant_id} en SARP.\n\n"
                "Comprueba que el número esté correctamente escrito o inténtalo "
                "nuevamente cuando la orden ya figure en el sistema."
            ),
        )
    return _alert(
        "⚠️ No se pudo consultar la orden",
        "SARP no respondió en este momento. Puedes volver a intentar la consulta.",
    )


def _warrant_type_label(types: tuple[str, ...]) -> str:
    normalized = {value.casefold() for value in types}
    if {"arrest", "search"}.issubset(normalized) or "both" in normalized:
        return "⚖️ Arresto y allanamiento"
    if "search" in normalized:
        return "🔍 Allanamiento"
    if "arrest" in normalized:
        return "🚓 Arresto"
    return "Sin especificar"


def _status_label(detail: WarrantDetail) -> str:
    if detail.closed_at is not None:
        return "Cerrada"
    if detail.status != 1:
        return "Inactiva"
    if detail.signed_at is not None:
        return "Activa · Firmada"
    return "Activa · Pendiente de firma"


def _risk_label(value: str | None) -> str:
    normalized = (value or "").casefold()
    return {
        "low": "Bajo",
        "medium": "Medio",
        "high": "Alto",
    }.get(normalized, value.title() if value else "Sin especificar")


def _process_lines(detail: WarrantDetail) -> list[str]:
    lines: list[str] = []
    if detail.filed_name:
        lines.append(f"Presentada por: {detail.filed_name}")
    if detail.created_at:
        lines.append(f"Creada: {_discord_time(detail.created_at)}")
    if detail.signed_name and detail.signed_at:
        lines.append(f"Firmada por: {detail.signed_name} · {_discord_time(detail.signed_at)}")
    if detail.closed_name and detail.closed_at:
        lines.append(f"Cerrada por: {detail.closed_name} · {_discord_time(detail.closed_at)}")
    elif detail.closed_at:
        lines.append(f"Cerrada: {_discord_time(detail.closed_at)}")
    if detail.updated_at and detail.updated_at != detail.created_at:
        lines.append(f"Actualizada: {_discord_time(detail.updated_at)}")
    return lines


def _tree_lines(lines: list[str]) -> str:
    return "\n".join(
        f"{'└' if index == len(lines) - 1 else '├'} {_safe_display_text(line)}"
        for index, line in enumerate(lines)
    )


def _field_chunks(value: str, *, max_total: int) -> tuple[str, ...]:
    normalized = value.strip()
    if len(normalized) > max_total:
        normalized = normalized[: max_total - 1].rstrip() + "…"
    chunks: list[str] = []
    remaining = normalized
    while remaining:
        if len(remaining) <= MAX_FIELD_CHARS:
            chunks.append(remaining)
            break
        split_at = remaining.rfind("\n", 0, MAX_FIELD_CHARS + 1)
        if split_at < MAX_FIELD_CHARS // 2:
            split_at = remaining.rfind(" ", 0, MAX_FIELD_CHARS + 1)
        if split_at <= 0:
            split_at = MAX_FIELD_CHARS
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    return tuple(chunks or ("No hay una descripción registrada para esta orden.",))


def _charge_lines(detail: WarrantDetail) -> str:
    if not detail.charges:
        return "└ No hay cargos registrados."
    lines: list[str] = []
    for index, charge in enumerate(detail.charges):
        prefix = "└" if index == len(detail.charges) - 1 else "├"
        code = f"`{_safe_display_text(charge.code)}` · " if charge.code else ""
        severity = f" · {charge.severity.title()}" if charge.severity else ""
        lines.append(f"{prefix} {code}{_safe_display_text(charge.name)}{severity}")
    rendered = "\n".join(lines)
    return rendered[:MAX_FIELD_CHARS]


def _detail_color(detail: WarrantDetail) -> discord.Color:
    if detail.closed_at is not None or detail.status != 1:
        return discord.Color.dark_grey()
    return {
        "high": discord.Color.red(),
        "medium": discord.Color.orange(),
        "low": discord.Color.green(),
    }.get((detail.risk or "").casefold(), discord.Color.blurple())


def _discord_time(value: datetime) -> str:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return f"<t:{int(aware.timestamp())}:f>"


def _safe_display_text(value: str) -> str:
    return discord.utils.escape_markdown(discord.utils.escape_mentions(value))


def _command_success(warrant_id: int) -> discord.Embed:
    return discord.Embed(
        title="✅ Notificación enviada",
        description=(f"La orden N.º {warrant_id} fue enviada correctamente a los encargados."),
        color=discord.Color.green(),
    )


def _command_failure() -> discord.Embed:
    return discord.Embed(
        title="⚠️ No pude enviar la notificación",
        color=discord.Color.red(),
    )


def _alert(title: str, description: str) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=discord.Color.red())


def _has_any_role(member: discord.Member, role_ids: tuple[int, ...]) -> bool:
    allowed = set(role_ids)
    return bool(allowed and any(role.id in allowed for role in member.roles))


def _log_command_error(
    event: str,
    error: Exception,
    ctx: discord.ApplicationContext,
) -> None:
    log_event(
        LOGGER,
        "error",
        event,
        exc_info=error,
        guild_id=getattr(ctx.guild, "id", None),
        channel_id=ctx.channel_id,
        user_id=getattr(ctx.author, "id", None),
        **exception_fields(error),
    )

"""Read SARP once, notify configured supervisors, and reuse the result as agent context."""

from __future__ import annotations

import hashlib
import logging

import discord

from arrest_report_agent.application.ports import ArrestReportDetail
from arrest_report_agent.config import ArrestReportLookupOptions
from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.infrastructure.sarp import SarpClient

from .lookup import ArrestWarrantCheck, SarpArrestWarrantLookup
from .repository import ArrestWarrantAlertRepository

LOGGER = logging.getLogger(__name__)


class ArrestWarrantMonitor:
    def __init__(
        self,
        bot: discord.Bot,
        client: SarpClient,
        repository: ArrestWarrantAlertRepository,
    ) -> None:
        self.bot = bot
        self.client = client
        self.repository = repository
        self.inspector = SarpArrestWarrantLookup(client)

    async def inspect(self, report: ArrestReportDetail) -> ArrestWarrantCheck:
        return await self.inspector.inspect(report)

    async def check_and_notify(
        self,
        *,
        guild_id: int,
        report: ArrestReportDetail,
        report_url: str,
        options: ArrestReportLookupOptions | None,
    ) -> str:
        check = await self.inspect(report)
        notification_status = "not_needed"
        if check.warrants and options is not None and options.warrant_alerts_enabled:
            notification_status = "delivered_or_previously_delivered"
            for channel_id in options.warrant_alert_channel_ids:
                try:
                    await self._notify(guild_id, report, report_url, options, channel_id, check)
                except Exception as error:
                    notification_status = "incomplete"
                    self._failure("arrest_warrant_alert_failed", error, report)
        elif check.warrants:
            notification_status = "disabled"
        log_event(
            LOGGER,
            "info",
            "arrest_warrant_checked",
            guild_id=guild_id,
            sarp_arrest_id=report.report_id,
            status=check.status,
            open_warrants=len(check.warrants),
            notification_status=notification_status,
        )
        return check.context(report, notification_status=notification_status)

    async def _notify(
        self,
        guild_id: int,
        report: ArrestReportDetail,
        report_url: str,
        options: ArrestReportLookupOptions,
        channel_id: int,
        check: ArrestWarrantCheck,
    ) -> None:
        guild = self.bot.get_guild(guild_id)
        if guild is None or options.warrant_alert_role_id is None:
            raise ValueError("Warrant alert guild or role is unavailable")
        role = guild.get_role(options.warrant_alert_role_id)
        channel = guild.get_channel(channel_id) or await guild.fetch_channel(channel_id)
        if role is None or not isinstance(channel, discord.TextChannel) or channel.guild.id != guild_id:
            raise ValueError("Warrant alert requires a configured role and same-guild text channel")
        if not role.mentionable and not channel.permissions_for(guild.me).mention_everyone:
            raise ValueError("Bot cannot mention the configured supervisory role")
        by_id = {item.warrant_id: item for item in check.warrants}

        async def send(warrant_ids: tuple[int, ...]) -> int:
            def safe(value: str, limit: int = 80) -> str:
                return discord.utils.escape_markdown(discord.utils.escape_mentions(value))[:limit]

            embed = discord.Embed(
                title=f"📋 Órdenes de arresto abiertas · Reporte #{report.report_id}",
                description=(
                    f"**Sospechoso:** {safe(report.suspect_name)}\n"
                    f"[Abrir expediente]({report_url})\n"
                    "Gestiona el cierre de estas órdenes. "
                    "El reporte requiere correcciones mientras permanezcan abiertas."
                ),
                color=discord.Color.orange(),
            )
            for warrant_id in warrant_ids:
                detail = by_id[warrant_id]
                if warrant_id in check.unavailable_details:
                    signature = "Firma no disponible"
                elif detail.signed_at is None:
                    signature = "Pendiente de firma"
                else:
                    signature = (
                        f"Firmada por {safe(detail.signed_name or 'No informado')} · "
                        f"{discord.utils.format_dt(detail.signed_at, style='f')}"
                    )
                created = (
                    discord.utils.format_dt(detail.created_at, style="f")
                    if detail.created_at is not None
                    else "No informado"
                )
                charges = "; ".join(charge.name for charge in detail.charges) or "No informados"
                embed.add_field(
                    name=(
                        f"Orden #{warrant_id} · Abierta según listado"
                        if warrant_id in check.unavailable_details
                        else f"Orden #{warrant_id} · Abierta"
                    ),
                    value=(
                        f"**Presentada por:** {safe(detail.filed_name or 'No informado')}\n"
                        f"**Firma:** {signature}\n**Creada:** {created}\n"
                        f"**Riesgo:** {safe(detail.risk or 'No informado', 30)}\n"
                        f"**Cargos:** {safe(charges, 120)}"
                    )[:500],
                    inline=False,
                )
            notice = " · Datos parciales" if check.status != "confirmed" else ""
            embed.set_footer(text=f"Consulta SARP: {check.checked_at.isoformat()}{notice}")
            nonce_source = f"{guild_id}:{report.report_id}:{channel_id}:{','.join(map(str, warrant_ids))}"
            nonce = int.from_bytes(hashlib.sha256(nonce_source.encode()).digest()[:8], "big") >> 1
            message = await channel.send(
                content=role.mention,
                embed=embed,
                nonce=nonce,
                enforce_nonce=True,
                allowed_mentions=discord.AllowedMentions(
                    everyone=False,
                    users=False,
                    roles=[discord.Object(id=role.id)],
                    replied_user=False,
                ),
            )
            return message.id

        await self.repository.deliver_once(
            guild_id=guild_id,
            report_id=report.report_id,
            channel_id=channel_id,
            warrant_ids=tuple(by_id),
            send=send,
        )

    @staticmethod
    def _failure(event: str, error: Exception, report: ArrestReportDetail) -> None:
        log_event(LOGGER, "warning", event, sarp_arrest_id=report.report_id, **exception_fields(error))

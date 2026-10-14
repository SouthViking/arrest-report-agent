"""Read SARP once, notify configured supervisors, and reuse the result as agent context."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

import discord

from arrest_report_agent.application.ports import ArrestReportDetail
from arrest_report_agent.config import ArrestReportLookupOptions
from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.infrastructure.sarp import SarpClient, SarpWarrant
from arrest_report_agent.warrant_notifications.service import (
    WarrantChargeDetail,
    WarrantDetail,
    WarrantLookupService,
)

from .repository import ArrestWarrantAlertRepository

LOGGER = logging.getLogger(__name__)
MAX_PAGES = 3
MAX_DETAILS = 10  # Bound detail requests and context size; one grouped role ping.


def _is_arrest(types: tuple[str, ...]) -> bool:
    return bool({value.casefold() for value in types}.intersection({"arrest", "both"}))


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@dataclass(frozen=True)
class ArrestWarrantCheck:
    checked_at: datetime
    status: Literal["confirmed", "incomplete", "unavailable"]
    warrants: tuple[WarrantDetail, ...] = ()
    unavailable_details: tuple[int, ...] = ()

    def context(self, report: ArrestReportDetail, *, notification_status: str) -> str:
        items = []
        for warrant in self.warrants:
            item = warrant.model_dump(mode="json")
            item["description"] = warrant.description[:1500]
            item["charges"] = item["charges"][:20]
            item["description_truncated"] = len(warrant.description) > 1500
            item["charges_truncated"] = len(warrant.charges) > 20
            item["detail_available"] = warrant.warrant_id not in self.unavailable_details
            item["predates_arrest"] = (
                warrant.created_at <= (_utc(report.created_at) or report.created_at)
                if warrant.created_at is not None
                else None
            )
            items.append(item)
        return "[SARP_ARREST_WARRANT_CONTEXT]\n" + json.dumps(
            {
                "report_id": report.report_id,
                "suspect_name": report.suspect_name,
                "arrest_record_created_at": report.created_at.isoformat(),
                "checked_at": self.checked_at.isoformat(),
                "status": self.status,
                "supervisor_notification": notification_status,
                "open_arrest_warrants": items,
            },
            ensure_ascii=False,
        )


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
        self.lookup = WarrantLookupService(client)

    async def inspect(self, report: ArrestReportDetail) -> ArrestWarrantCheck:
        now = datetime.now(UTC)
        if report.suspect_id is None:
            return ArrestWarrantCheck(now, "unavailable")
        candidates: dict[int, SarpWarrant] = {}
        incomplete = False
        try:
            for page_number in range(1, MAX_PAGES + 1):
                page = await self.client.list_active_warrants(report.suspect_id, page=page_number, limit=100)
                if page.total and not page.items:
                    incomplete = True
                for item in page.items:
                    if item.suspect_id != report.suspect_id:
                        incomplete = True
                        continue
                    if item.active and _is_arrest(item.types):
                        candidates[item.id] = item
                    elif item.active and not item.types:
                        incomplete = True
                if page_number >= page.pages:
                    break
            else:
                incomplete = True
        except Exception as error:
            self._failure("arrest_warrant_list_failed", error, report)
            if not candidates:
                return ArrestWarrantCheck(now, "unavailable")
            incomplete = True
        warrants = []
        unavailable_details = []
        if len(candidates) > MAX_DETAILS:
            incomplete = True
        for candidate in sorted(candidates.values(), key=lambda item: item.id)[:MAX_DETAILS]:
            # The detail may have changed since the list: confirm identity, type and closure again.
            try:
                payload = await self.client.get_warrant(candidate.id)
                if payload.suspect_id != report.suspect_id or payload.id != candidate.id:
                    incomplete = True
                    continue
                if payload.status != 1 or payload.closed_at is not None or not _is_arrest(payload.types):
                    continue
                detail = self.lookup.project(payload)
                detail = detail.model_copy(
                    update={
                        key: _utc(getattr(detail, key))
                        for key in ("created_at", "updated_at", "signed_at", "closed_at")
                    }
                )
            except Exception as error:
                self._failure("arrest_warrant_detail_failed", error, report)
                incomplete = True
                unavailable_details.append(candidate.id)
                # The list confirmed an open arrest warrant; do not invent the missing signature or issuer.
                detail = WarrantDetail(
                    warrant_id=candidate.id,
                    suspect_name=report.suspect_name,
                    description=candidate.description,
                    types=candidate.types,
                    risk=candidate.risk,
                    status=candidate.status,
                    created_at=_utc(candidate.created_at),
                    charges=tuple(
                        WarrantChargeDetail(
                            code=charge.code,
                            name=charge.charge,
                            severity=charge.type,
                            hours=charge.hours,
                            fine=charge.fine,
                        )
                        for charge in candidate.charges
                    ),
                )
            warrants.append(detail)
        return ArrestWarrantCheck(
            datetime.now(UTC),
            "incomplete" if incomplete else "confirmed",
            tuple(warrants),
            tuple(unavailable_details),
        )

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
                    "Verifica si corresponde cerrar estas órdenes. Su estado no bloquea la revisión."
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

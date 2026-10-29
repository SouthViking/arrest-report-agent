"""Deterministic, deduplicated internal alerts for open theft matches in report reviews."""

import asyncio
import logging

import discord

from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.domain import ReviewCase
from arrest_report_agent.infrastructure.postgres import DatabaseConfigProvider
from arrest_report_agent.stolen_weapons.models import StolenWeaponReport
from arrest_report_agent.stolen_weapons.repository import StolenWeaponRepository

LOGGER = logging.getLogger(__name__)


def build_theft_alert(
    case: ReviewCase, report: StolenWeaponReport, evidence_ids: tuple[int, ...]
) -> discord.Embed:
    review_url = (
        f"https://discord.com/channels/{case.guild_id}/"
        f"{case.parent_channel_id or case.channel_id}/{case.root_message_id}"
    )
    links = f"📋 [Ver revisión de arresto]({review_url})"
    if report.jump_url:
        links += f"\n🔗 [Ver registro del robo]({report.jump_url})"
    embed = discord.Embed(
        title="🔎 Coincidencia con arma robada",
        description="Se identificó un arma con un reporte de robo abierto durante una revisión de arresto.",
        color=discord.Color.orange(),
    )
    embed.add_field(name="Serial", value=discord.utils.escape_markdown(report.details.serial), inline=False)
    embed.add_field(
        name="Bolsa de evidencia" if len(evidence_ids) == 1 else "Bolsas de evidencia",
        value=", ".join(str(id) for id in evidence_ids),
        inline=False,
    )
    embed.add_field(name="Seguimiento", value=links, inline=False)
    embed.set_footer(text=report.reference)
    return embed


class WeaponTheftAlertNotifier:
    def __init__(
        self, bot: discord.Bot, provider: DatabaseConfigProvider, repository: StolenWeaponRepository
    ) -> None:
        self.bot, self.provider, self.repository = bot, provider, repository

    async def notify(self, case: ReviewCase, serials_by_bag: dict[int, tuple[str, ...]]) -> None:
        bundle = await self.provider.cached(case.guild_id)
        options = bundle.settings.stolen_weapon_options
        if options is None or not options.notifications_enabled:
            return
        allowed = bundle.settings.report_channel_allowlist.ids or frozenset()
        if case.parent_channel_id not in allowed:
            return
        guild = self.bot.get_guild(case.guild_id)
        channel = guild.get_channel(options.destination_channel_id) if guild else None
        if not isinstance(channel, discord.TextChannel) or guild is None or guild.me is None:
            return
        permissions = channel.permissions_for(guild.me)
        if not all((permissions.view_channel, permissions.send_messages, permissions.embed_links)):
            return
        serials = tuple(dict.fromkeys(serial for values in serials_by_bag.values() for serial in values))
        reports = await self.repository.find_by_serials(case.guild_id, serials)
        for report in reports:
            if report.status != "open":
                continue
            evidence_ids = tuple(
                sorted(
                    bag
                    for bag, values in serials_by_bag.items()
                    if report.details.normalized_serial in values
                )
            )
            if not evidence_ids or not await self.repository.claim_alert(
                case.guild_id,
                report.id,
                review_root_message_id=case.root_message_id,
                channel_id=channel.id,
                evidence_ids=evidence_ids,
            ):
                continue
            try:
                async with asyncio.timeout(10):
                    message = await channel.send(
                        embed=build_theft_alert(case, report, evidence_ids),
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
            except Exception as error:
                status = "failed" if isinstance(error, (discord.Forbidden, discord.NotFound)) else "unknown"
                await self.repository.mark_alert(
                    case.guild_id, report.id, case.root_message_id, status=status
                )
                log_event(
                    LOGGER,
                    "warning",
                    "weapon_theft_alert_failed",
                    guild_id=case.guild_id,
                    report_id=str(report.id),
                    review_root_message_id=case.root_message_id,
                    delivery_status=status,
                    **exception_fields(error),
                )
                continue
            await self.repository.mark_alert(
                case.guild_id, report.id, case.root_message_id, status="sent", message_id=message.id
            )
            log_event(
                LOGGER,
                "info",
                "weapon_theft_alert_sent",
                guild_id=case.guild_id,
                report_id=str(report.id),
                review_root_message_id=case.root_message_id,
                message_id=message.id,
                channel_id=channel.id,
            )

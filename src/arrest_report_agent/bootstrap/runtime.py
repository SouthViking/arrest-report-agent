"""Wire local knowledge, agent runner and Discord adapters."""

from __future__ import annotations

from dataclasses import dataclass

import discord
from agents import set_default_openai_key

from arrest_report_agent.application import ReviewService
from arrest_report_agent.config import AppSettings, ModelCatalog, ResolvedModel
from arrest_report_agent.infrastructure.discord import ReviewCog, create_bot
from arrest_report_agent.knowledge import ChargeCatalog, ReportGuide


@dataclass(frozen=True)
class Runtime:
    bot: discord.Bot
    catalog: ChargeCatalog
    guide: ReportGuide
    model: ResolvedModel

    async def health(self) -> dict[str, object]:
        discord_ready = self.bot.is_ready()
        knowledge_ready = self.catalog.charge_count > 0 and bool(self.guide.version)
        return {
            "ok": discord_ready and knowledge_ready,
            "discord": discord_ready,
            "knowledge": knowledge_ready,
            "charges": self.catalog.charge_count,
        }


def build_runtime(settings: AppSettings) -> Runtime:
    set_default_openai_key(settings.openai_api_key.get_secret_value(), use_for_tracing=True)
    catalog = ChargeCatalog.load_default()
    guide = ReportGuide.load_default()
    model = ModelCatalog.load_default().resolve(
        settings.openai_model,
        settings.openai_reasoning_effort,
    )
    bot = create_bot(settings)
    service = ReviewService(settings, catalog, guide, model)
    bot.add_cog(ReviewCog(bot, settings, service))
    return Runtime(bot=bot, catalog=catalog, guide=guide, model=model)

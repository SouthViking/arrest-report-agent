"""Wire local knowledge, agent runner and Discord adapters."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import discord
from agents import set_default_openai_key

from arrest_report_agent.application import ReviewService
from arrest_report_agent.config import (
    AppSettings,
    LoadedPersona,
    ModelCatalog,
    PackagedPersonaProvider,
    PersonaProvider,
    ResolvedModel,
    load_global_profile_banner,
)
from arrest_report_agent.config.logging import log_event
from arrest_report_agent.infrastructure.discord import (
    DiscordProfileSynchronizer,
    ReviewCog,
    create_bot,
)
from arrest_report_agent.knowledge import ChargeCatalog, ReportGuide
from arrest_report_agent.role_requests import (
    RoleRequestCog,
    RoleRequestDecisionView,
    RoleRequestService,
)

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Runtime:
    bot: discord.Bot
    catalog: ChargeCatalog
    guide: ReportGuide
    model: ResolvedModel
    persona: LoadedPersona
    role_requests_enabled: bool

    async def health(self) -> dict[str, object]:
        discord_ready = self.bot.is_ready()
        knowledge_ready = self.catalog.charge_count > 0 and bool(self.guide.version)
        return {
            "ok": discord_ready and knowledge_ready,
            "discord": discord_ready,
            "knowledge": knowledge_ready,
            "charges": self.catalog.charge_count,
            "persona": self.persona.profile.id,
            "persona_revision": self.persona.profile.revision,
            "role_requests": self.role_requests_enabled,
        }


async def build_runtime(
    settings: AppSettings,
    persona_provider: PersonaProvider | None = None,
) -> Runtime:
    persona = await (persona_provider or PackagedPersonaProvider()).load(settings.bot_persona)
    log_event(
        LOGGER,
        "info",
        "persona_loaded",
        persona_id=persona.profile.id,
        persona_revision=persona.profile.revision,
        persona_schema_version=persona.profile.schema_version,
    )
    set_default_openai_key(settings.openai_api_key.get_secret_value(), use_for_tracing=True)
    catalog = ChargeCatalog.load_default()
    guide = ReportGuide.load_default()
    model = ModelCatalog.load_default().resolve(
        settings.openai_model,
        settings.openai_reasoning_effort,
    )
    bot = create_bot(settings, persona.profile)
    service = ReviewService(settings, catalog, guide, model, persona.profile)
    profile_synchronizer = DiscordProfileSynchronizer(
        bot,
        persona,
        global_banner_bytes=load_global_profile_banner(),
    )
    bot.add_cog(ReviewCog(bot, settings, service, persona.profile, profile_synchronizer))
    role_requests_enabled = bool(settings.role_request_reviewer_role_ids)
    if role_requests_enabled:
        role_request_service = RoleRequestService(bot, settings, persona.profile)
        bot.add_view(RoleRequestDecisionView(role_request_service))
        bot.add_cog(RoleRequestCog(role_request_service))
        log_event(
            LOGGER,
            "info",
            "role_request_subsystem_registered",
            reviewer_role_count=len(settings.role_request_reviewer_role_ids),
            role_access_mode=(
                "restricted"
                if settings.role_request_allowed_role_allowlist.is_restricted
                else "open"
            ),
            configured_role_count=(
                settings.role_request_allowed_role_allowlist.configured_count
            ),
            channel_access_mode=(
                "restricted"
                if settings.role_request_allowed_channel_allowlist.is_restricted
                else "open"
            ),
            configured_channels=(
                settings.role_request_allowed_channel_allowlist.configured_count
            ),
        )
    return Runtime(
        bot=bot,
        catalog=catalog,
        guide=guide,
        model=model,
        persona=persona,
        role_requests_enabled=role_requests_enabled,
    )

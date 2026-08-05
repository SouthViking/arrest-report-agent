"""Process entry point."""

from __future__ import annotations

import asyncio
import logging

from agents import flush_traces

from arrest_report_agent.bootstrap import build_runtime
from arrest_report_agent.bootstrap.health import serve_health
from arrest_report_agent.config import load_settings
from arrest_report_agent.config.logging import configure_logging, log_event

LOGGER = logging.getLogger(__name__)


async def run() -> None:
    settings = load_settings()
    configure_logging(settings.log_level, json_indented=settings.log_json_indented)
    runtime = build_runtime(settings)
    health_server = await serve_health(settings.health_host, settings.health_port, runtime.health)
    guilds = settings.guild_allowlist
    channels = settings.channel_allowlist
    users = settings.user_allowlist
    supervisor_roles = settings.supervisor_role_allowlist
    log_event(
        LOGGER,
        "info",
        "runtime_started",
        environment=settings.app_environment,
        version=settings.app_version,
        model=runtime.model.id,
        reasoning_effort=runtime.model.reasoning_effort,
        service_tier=runtime.model.runtime_defaults.service_tier,
        model_catalog_schema_version=runtime.model.catalog_schema_version,
        pricing_version=runtime.model.pricing_version,
        guild_access_mode="restricted" if guilds.is_restricted else "open",
        configured_guilds=guilds.configured_count,
        channel_access_mode="restricted" if channels.is_restricted else "open",
        configured_channels=channels.configured_count,
        user_access_mode="restricted" if users.is_restricted else "open",
        configured_users=users.configured_count,
        supervisor_access_mode=(
            "restricted" if supervisor_roles.is_restricted else "open"
        ),
        configured_supervisor_roles=supervisor_roles.configured_count,
    )
    try:
        await runtime.bot.start(settings.discord_token.get_secret_value())
    finally:
        health_server.close()
        await health_server.wait_closed()
        if not runtime.bot.is_closed():
            await runtime.bot.close()
        flush_traces()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        LOGGER.info("Arrest report agent stopped")


if __name__ == "__main__":
    main()

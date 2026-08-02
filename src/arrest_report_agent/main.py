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
    configure_logging(settings.log_level)
    runtime = build_runtime(settings)
    health_server = await serve_health(settings.health_host, settings.health_port, runtime.health)
    log_event(
        LOGGER,
        "info",
        "runtime_started",
        environment=settings.app_environment,
        version=settings.app_version,
        model=settings.openai_model,
        guild_id=settings.discord_guild_id,
        channels=len(settings.review_channel_ids),
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


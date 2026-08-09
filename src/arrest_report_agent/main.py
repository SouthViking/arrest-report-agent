"""Process entry point."""

from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import suppress

from agents import flush_traces

from arrest_report_agent.bootstrap import build_runtime
from arrest_report_agent.bootstrap.health import serve_health
from arrest_report_agent.config import load_settings
from arrest_report_agent.config.logging import configure_logging, exception_fields, log_event

LOGGER = logging.getLogger(__name__)


async def run() -> None:
    settings = load_settings()
    configure_logging(settings.log_level, json_indented=settings.log_json_indented)
    try:
        runtime = await build_runtime(settings)
    except Exception as error:
        log_event(
            LOGGER,
            "error",
            "runtime_start_failed",
            exc_info=error,
            persona_id=settings.bot_persona,
            **exception_fields(error),
        )
        raise
    health_server = await serve_health(settings.health_host, settings.health_port, runtime.health)
    guilds = settings.guild_allowlist
    report_channels = settings.report_channel_allowlist
    users = settings.user_allowlist
    supervisor_roles = settings.supervisor_role_allowlist
    role_request_channels = settings.role_request_allowed_channel_allowlist
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
        persona_id=runtime.persona.profile.id,
        persona_revision=runtime.persona.profile.revision,
        guild_access_mode="restricted" if guilds.is_restricted else "open",
        configured_guilds=guilds.configured_count,
        report_channel_mode="restricted" if report_channels.is_restricted else "open",
        configured_report_channels=report_channels.configured_count,
        user_access_mode="restricted" if users.is_restricted else "open",
        configured_users=users.configured_count,
        supervisor_access_mode=(
            "restricted" if supervisor_roles.is_restricted else "open"
        ),
        configured_supervisor_roles=supervisor_roles.configured_count,
        role_request_subsystem_enabled=runtime.role_requests_enabled,
        role_request_channel_mode=(
            "restricted" if role_request_channels.is_restricted else "open"
        ),
        configured_role_request_channels=role_request_channels.configured_count,
        configured_role_request_reviewers=len(settings.role_request_reviewer_role_ids),
    )
    stop_requested = asyncio.Event()
    loop = asyncio.get_running_loop()
    registered_signals: list[signal.Signals] = []
    for shutdown_signal in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(shutdown_signal, stop_requested.set)
            registered_signals.append(shutdown_signal)

    bot_task = asyncio.create_task(
        runtime.bot.start(settings.discord_token.get_secret_value()),
        name="discord-bot",
    )
    stop_task = asyncio.create_task(stop_requested.wait(), name="shutdown-signal")
    try:
        done, _ = await asyncio.wait(
            {bot_task, stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if stop_task in done and not bot_task.done():
            log_event(LOGGER, "info", "runtime_shutdown_requested")
            await runtime.bot.close()
        await bot_task
    finally:
        stop_task.cancel()
        await asyncio.gather(stop_task, return_exceptions=True)
        for shutdown_signal in registered_signals:
            loop.remove_signal_handler(shutdown_signal)
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

"""Process entry point."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
from contextlib import suppress

from agents import flush_traces

from arrest_report_agent.bootstrap import build_runtime
from arrest_report_agent.bootstrap.database import run_import_command, run_verify_command
from arrest_report_agent.bootstrap.health import serve_http
from arrest_report_agent.config import AppSettings, load_settings
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
    health_server = await serve_http(
        settings.health_host,
        settings.health_port,
        runtime.health,
        runtime.railway_webhook_receiver,
    )
    guild_ids = await runtime.config_provider.guild_ids()
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
        configuration_source="postgresql",
        configured_guilds=len(guild_ids),
        role_request_subsystem_enabled=runtime.role_requests_enabled,
        pager_subsystem_enabled=runtime.pagers_enabled,
        deployment_notification_subsystem_enabled=(
            runtime.deployment_notifications_enabled
        ),
        railway_webhook_enabled=runtime.railway_webhook_receiver is not None,
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
        await runtime.close()
        flush_traces()


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="arrest-report-agent")
    subcommands = parser.add_subparsers(dest="command")
    database = subcommands.add_parser("db", help="Database migration and bootstrap commands")
    database_commands = database.add_subparsers(dest="database_command", required=True)
    database_commands.add_parser("migrate", help="Apply all Alembic migrations")
    import_environment = database_commands.add_parser(
        "import-env",
        help="Import the legacy environment configuration",
    )
    mode = import_environment.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    import_environment.add_argument("--replace", action="store_true")
    database_commands.add_parser("verify", help="Verify database and persona assets")
    return parser


def _run_migrations(settings: AppSettings) -> None:
    from alembic import command
    from alembic.config import Config

    postgres_url = settings.postgres_url
    os.environ["DATABASE_URL"] = postgres_url
    command.upgrade(Config("alembic.ini"), "head")


async def _run_database_command(args: argparse.Namespace) -> None:
    settings = load_settings()
    configure_logging(settings.log_level, json_indented=settings.log_json_indented)
    if args.database_command == "migrate":
        await asyncio.to_thread(_run_migrations, settings)
        result: dict[str, object] = {"status": "migrated", "revision": "head"}
    elif args.database_command == "import-env":
        result = await run_import_command(
            settings,
            apply=bool(args.apply),
            replace=bool(args.replace),
        )
    elif args.database_command == "verify":
        result = await run_verify_command(settings)
    else:  # pragma: no cover - argparse enforces the command choices.
        raise RuntimeError(f"Unsupported database command {args.database_command}")
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


def main() -> None:
    args = _build_parser().parse_args()
    try:
        if args.command == "db":
            asyncio.run(_run_database_command(args))
        else:
            asyncio.run(run())
    except KeyboardInterrupt:
        LOGGER.info("Arrest report agent stopped")


if __name__ == "__main__":
    main()

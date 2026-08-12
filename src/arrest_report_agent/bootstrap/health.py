"""Small aiohttp server for health probes and authenticated provider webhooks."""

from __future__ import annotations

import hmac
import json
from collections.abc import Awaitable, Callable

from aiohttp import web
from pydantic import ValidationError

from arrest_report_agent.deployments import (
    RailwayDeploymentEvent,
    RailwayWebhookPayloadError,
    RailwayWebhookReceiver,
)
from arrest_report_agent.infrastructure.postgres import DatabaseUnavailableError

HealthCheck = Callable[[], Awaitable[dict[str, object]]]
MAX_WEBHOOK_BYTES = 65_536
RAILWAY_BROWSER_ORIGINS = frozenset({"https://railway.com", "https://railway.app"})


class HttpServer:
    """Lifecycle wrapper retaining the former health-server close interface."""

    def __init__(self, runner: web.AppRunner, site: web.TCPSite) -> None:
        self.runner = runner
        self.site = site

    def close(self) -> None:
        """Compatibility no-op; cleanup occurs in ``wait_closed``."""

    async def wait_closed(self) -> None:
        await self.runner.cleanup()


async def serve_http(
    host: str,
    port: int,
    check: HealthCheck,
    webhook_receiver: RailwayWebhookReceiver | None = None,
) -> HttpServer:
    app = create_http_application(check, webhook_receiver)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    return HttpServer(runner, site)


async def serve_health(host: str, port: int, check: HealthCheck) -> HttpServer:
    """Backwards-compatible health-only server used by focused tests."""

    return await serve_http(host, port, check)


def create_http_application(
    check: HealthCheck,
    webhook_receiver: RailwayWebhookReceiver | None = None,
) -> web.Application:
    app = web.Application(client_max_size=MAX_WEBHOOK_BYTES)

    async def health(_request: web.Request) -> web.Response:
        payload = await check()
        return web.json_response(payload, status=200 if payload.get("ok") else 503)

    app.router.add_get("/health", health)
    if webhook_receiver is not None:
        path = "/webhooks/railway/{secret}"

        async def railway_webhook(request: web.Request) -> web.Response:
            if not hmac.compare_digest(
                request.match_info["secret"],
                webhook_receiver.secret,
            ):
                raise web.HTTPNotFound()
            cors_headers = _cors_headers(request)
            if request.method == "OPTIONS":
                return web.Response(status=204, headers=cors_headers)
            if request.content_type != "application/json":
                return web.json_response(
                    {"status": "invalid", "error": "application_json_required"},
                    status=415,
                    headers=cors_headers,
                )
            if request.content_length is not None and request.content_length > MAX_WEBHOOK_BYTES:
                raise web.HTTPRequestEntityTooLarge(
                    max_size=MAX_WEBHOOK_BYTES,
                    actual_size=request.content_length,
                )
            try:
                raw_body = await request.read()
                payload = json.loads(raw_body)
                event = RailwayDeploymentEvent.model_validate(payload)
                acceptance = await webhook_receiver.receive(event)
            except (json.JSONDecodeError, UnicodeDecodeError, ValidationError, RailwayWebhookPayloadError):
                return web.json_response(
                    {"status": "invalid", "error": "unsupported_payload"},
                    status=400,
                    headers=cors_headers,
                )
            except DatabaseUnavailableError:
                return web.json_response(
                    {"status": "unavailable"},
                    status=503,
                    headers=cors_headers,
                )
            return web.json_response(
                {"status": acceptance.status},
                status=202,
                headers=cors_headers,
            )

        app.router.add_route("POST", path, railway_webhook)
        app.router.add_route("OPTIONS", path, railway_webhook)
    return app


def _cors_headers(request: web.Request) -> dict[str, str]:
    origin = request.headers.get("Origin")
    if origin not in RAILWAY_BROWSER_ORIGINS:
        return {}
    return {
        "Access-Control-Allow-Origin": origin,
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Vary": "Origin",
    }

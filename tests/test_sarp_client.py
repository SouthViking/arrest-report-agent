from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import pytest
from aiohttp import web
from pydantic import BaseModel, TypeAdapter

from arrest_report_agent.infrastructure.sarp import (
    SarpApiError,
    SarpAuthenticationError,
    SarpBadRequestError,
    SarpClient,
    SarpClientConfig,
    SarpClientNotOpenError,
    SarpConflictError,
    SarpForbiddenError,
    SarpInvalidResponseError,
    SarpNotFoundError,
    SarpPenalCodePayload,
    SarpPenalCodeProvider,
    SarpRateLimitError,
    SarpServerError,
    build_charge_catalog,
)
from arrest_report_agent.knowledge import ChargeCatalog, ChargeCatalogSource
from arrest_report_agent.knowledge.catalog import Charge, ChargeCategory


class ProbePayload(BaseModel):
    ok: bool


Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]
PROBE_ADAPTER = TypeAdapter(ProbePayload)


def penal_code_payload(*, charge_id: int = 91, title: str = "Evasión") -> SarpPenalCodePayload:
    return SarpPenalCodePayload.model_validate(
        {
            "categories": [
                {
                    "id": 4,
                    "title": "Delitos contra la autoridad",
                    "description": "Delitos que afectan el procedimiento policial.",
                    "min_code": "CH-4100",
                    "max_code": "CH-4199",
                }
            ],
            "charges": [
                {
                    "id": charge_id,
                    "category": 4,
                    "category_title": "Delitos contra la autoridad",
                    "code": "CH-4102",
                    "charge": title,
                    "description": "Huir después de una indicación policial.",
                    "type": "Felony",
                    "time": 45,
                    "hours": 45,
                    "parole": 10,
                    "fine": 2500,
                    "points": 2,
                }
            ],
        }
    )


def local_catalog() -> ChargeCatalog:
    return ChargeCatalog(
        [
            ChargeCategory(
                id=1,
                title="Local",
                charges=[
                    Charge(
                        id=1,
                        category=1,
                        code="LOCAL-1",
                        charge="Respaldo local",
                        type="Local",
                        description="Catálogo empaquetado.",
                    )
                ],
            )
        ],
        "local-hash",
    )


class FakePenalCodeClient:
    def __init__(self, results: list[SarpPenalCodePayload | Exception]) -> None:
        self.results = results

    async def open(self) -> None:
        return None

    async def close(self) -> None:
        return None

    async def get_penal_code(self) -> SarpPenalCodePayload:
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


@asynccontextmanager
async def serve_sarp(handler: Handler) -> AsyncIterator[str]:
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    assert site._server is not None
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}/api"
    finally:
        await runner.cleanup()


async def test_client_authenticates_normalizes_path_and_parses_typed_payload(
    caplog: pytest.LogCaptureFixture,
) -> None:
    token = "sarp_super_secret"

    async def handler(request: web.Request) -> web.Response:
        assert request.path == "/api/probe"
        assert request.query == {"page": "1"}
        assert request.headers["Authorization"] == f"Bearer {token}"
        assert request.headers["Accept"] == "application/json"
        return web.json_response(
            {
                "message": "OK",
                "httpCode": 200,
                "source": "test",
                "payload": {"ok": True},
            },
            headers={
                "RateLimit": "limit=60, remaining=59",
                "RateLimit-Policy": "60;w=60",
                "RateLimit-Reset": "42",
                "X-Request-ID": "request-123",
            },
        )

    async with serve_sarp(handler) as base_url:
        caplog.set_level(logging.DEBUG)
        config = SarpClientConfig(token=token, base_url=base_url, max_retries=0)
        assert token not in repr(config)
        async with SarpClient(config) as client:
            response = await client.request(
                "GET",
                "/api/probe",
                params={"page": 1, "ignored": None},
                payload_adapter=PROBE_ADAPTER,
            )

    assert response.payload == ProbePayload(ok=True)
    assert response.source == "test"
    assert response.request_id == "request-123"
    assert response.rate_limit.limit == "limit=60, remaining=59"
    assert response.rate_limit.policy == "60;w=60"
    assert response.rate_limit.reset == "42"
    assert token not in caplog.text


async def test_get_penal_code_uses_documented_endpoint_and_returns_typed_payload() -> None:
    async def handler(request: web.Request) -> web.Response:
        assert request.path == "/api/police/penal-code"
        return web.json_response(
            {
                "message": "OK",
                "httpCode": 200,
                "source": "pcu",
                "payload": penal_code_payload().model_dump(mode="json"),
            }
        )

    async with serve_sarp(handler) as base_url, SarpClient(
        SarpClientConfig(token="sarp_test", base_url=base_url, max_retries=0)
    ) as client:
        payload = await client.get_penal_code()

    assert payload.categories[0].id == 4
    assert payload.charges[0].charge == "Evasión"


async def test_penal_code_keeps_uncategorized_charges_returned_by_current_api() -> None:
    payload = SarpPenalCodePayload.model_validate(
        {
            "categories": [
                {
                    "id": 4,
                    "title": "Delitos contra la autoridad",
                }
            ],
            "charges": [
                {
                    "id": 89,
                    "category": 0,
                    "category_title": None,
                    "code": "CP-189",
                    "charge": "Homicidio en primer grado",
                    "type": "GRAVE",
                }
            ],
        }
    )

    catalog = build_charge_catalog(payload)

    assert catalog.charge_count == 1
    assert catalog.get_details([89])["missing_ids"] == []
    assert catalog.list_index(query="Homicidio")["results"][0]["category"] == "Sin categoría"


async def test_penal_code_provider_uses_remote_then_last_valid_snapshot_on_failure() -> None:
    fallback = local_catalog()
    client = FakePenalCodeClient(
        [
            penal_code_payload(),
            RuntimeError("temporary outage"),
        ]
    )
    provider = SarpPenalCodeProvider(client, fallback)

    remote = await provider.refresh()
    cached = await provider.refresh()

    assert remote.source is ChargeCatalogSource.SARP
    assert remote.catalog.charge_count == 1
    assert remote.catalog.get_details([91])["missing_ids"] == []
    assert cached.source is ChargeCatalogSource.SARP_CACHED
    assert cached.catalog is remote.catalog


async def test_penal_code_provider_keeps_local_catalog_until_remote_is_valid() -> None:
    fallback = local_catalog()
    provider = SarpPenalCodeProvider(
        FakePenalCodeClient([RuntimeError("unavailable")]),
        fallback,
    )

    snapshot = await provider.refresh()

    assert snapshot.source is ChargeCatalogSource.LOCAL
    assert snapshot.catalog is fallback


@pytest.mark.parametrize(
    ("status", "message", "error_type"),
    [
        (400, "bad_request", SarpBadRequestError),
        (401, "unauthorized", SarpAuthenticationError),
        (403, "forbidden", SarpForbiddenError),
        (404, "not_found", SarpNotFoundError),
        (409, "conflict", SarpConflictError),
        (429, "too_many_requests", SarpRateLimitError),
        (500, "internal_server_error", SarpServerError),
    ],
)
async def test_client_maps_every_documented_error(
    status: int,
    message: str,
    error_type: type[SarpApiError],
) -> None:
    async def handler(_request: web.Request) -> web.Response:
        return web.json_response(
            {"message": message, "httpCode": status, "source": "test"},
            status=status,
            headers={"Retry-After": "3"},
        )

    async with serve_sarp(handler) as base_url, SarpClient(
        SarpClientConfig(base_url=base_url, max_retries=0)
    ) as client:
        with pytest.raises(error_type) as captured:
            await client.request("GET", "/probe", payload_adapter=PROBE_ADAPTER)

    assert captured.value.status_code == status
    assert captured.value.message == message
    assert captured.value.rate_limit.retry_after_seconds == 3


async def test_client_retries_safe_requests_using_retry_after() -> None:
    calls = 0
    delays: list[float] = []

    async def handler(_request: web.Request) -> web.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return web.json_response(
                {"message": "too_many_requests", "httpCode": 429},
                status=429,
                headers={"Retry-After": "0"},
            )
        return web.json_response(
            {"message": "OK", "httpCode": 200, "payload": {"ok": True}},
        )

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    async with serve_sarp(handler) as base_url, SarpClient(
        SarpClientConfig(base_url=base_url, max_retries=1),
        sleep=record_sleep,
    ) as client:
        response = await client.request("GET", "/probe", payload_adapter=PROBE_ADAPTER)

    assert response.payload.ok is True
    assert calls == 2
    assert delays == [0]


async def test_client_never_retries_writes_implicitly() -> None:
    calls = 0

    async def handler(_request: web.Request) -> web.Response:
        nonlocal calls
        calls += 1
        return web.json_response(
            {"message": "internal_server_error", "httpCode": 500},
            status=500,
        )

    async with serve_sarp(handler) as base_url, SarpClient(
        SarpClientConfig(base_url=base_url, max_retries=3)
    ) as client:
        with pytest.raises(SarpServerError):
            await client.request(
                "POST",
                "/probe",
                json_body={"example": True},
                payload_adapter=PROBE_ADAPTER,
            )

    assert calls == 1


@pytest.mark.parametrize(
    ("response_kind", "expected_message"),
    [
        ("html", "non-JSON"),
        ("mismatched_status", "does not match"),
        ("invalid_payload", "endpoint payload"),
    ],
)
async def test_client_rejects_responses_outside_the_documented_contract(
    response_kind: str,
    expected_message: str,
) -> None:
    async def handler(_request: web.Request) -> web.Response:
        if response_kind == "html":
            return web.Response(status=403, text="<html>challenge</html>")
        if response_kind == "mismatched_status":
            return web.json_response(
                {"message": "OK", "httpCode": 201, "payload": {"ok": True}}
            )
        return web.json_response(
            {"message": "OK", "httpCode": 200, "payload": {"ok": "not-a-bool"}}
        )

    async with serve_sarp(handler) as base_url, SarpClient(
        SarpClientConfig(base_url=base_url, max_retries=0)
    ) as client:
        with pytest.raises(SarpInvalidResponseError, match=expected_message):
            await client.request("GET", "/probe", payload_adapter=PROBE_ADAPTER)


async def test_client_requires_open_lifecycle_and_rejects_absolute_paths() -> None:
    client = SarpClient(SarpClientConfig())

    with pytest.raises(SarpClientNotOpenError):
        await client.request("GET", "/probe", payload_adapter=PROBE_ADAPTER)
    await client.open()
    try:
        with pytest.raises(ValueError, match="relative"):
            await client.request(
                "GET",
                "https://attacker.invalid/steal",
                payload_adapter=PROBE_ADAPTER,
            )
    finally:
        await client.close()

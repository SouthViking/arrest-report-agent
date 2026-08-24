from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import date

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
    SarpFactionDetail,
    SarpFactionDivision,
    SarpFactionRank,
    SarpForbiddenError,
    SarpGlobalMetrics,
    SarpInvalidResponseError,
    SarpNotFoundError,
    SarpPenalCodePayload,
    SarpPenalCodeProvider,
    SarpRateLimitError,
    SarpServerCacheInfo,
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

    async with (
        serve_sarp(handler) as base_url,
        SarpClient(SarpClientConfig(token="sarp_test", base_url=base_url, max_retries=0)) as client,
    ):
        payload = await client.get_penal_code()

    assert payload.categories[0].id == 4
    assert payload.charges[0].charge == "Evasión"


async def test_global_metrics_endpoint_returns_connected_account_counter() -> None:
    async def handler(request: web.Request) -> web.Response:
        assert request.path == "/api/public/global-metrics"
        return web.json_response(
            {
                "message": "OK",
                "httpCode": 200,
                "source": "home",
                "payload": {
                    "onlineAccountCounter": 109,
                    "weeklyPeakPlayers": 200,
                    "accountCounter": 13250,
                },
            }
        )

    async with (
        serve_sarp(handler) as base_url,
        SarpClient(SarpClientConfig(token="sarp_test", base_url=base_url, max_retries=0)) as client,
    ):
        metrics = await client.get_global_metrics()

    assert metrics == SarpGlobalMetrics(onlineAccountCounter=109)


async def test_server_cache_endpoint_returns_validated_artifact_metadata() -> None:
    async def handler(request: web.Request) -> web.Response:
        assert request.path == "/api/public/server-cache/download"
        return web.json_response(
            {
                "message": "OK",
                "httpCode": 200,
                "source": "home",
                "payload": {
                    "exists": True,
                    "filename": "server-cache.zip",
                    "url": "https://cdn.example.com/server-cache.zip?v=27",
                    "sizeBytes": 822_146_470,
                    "lastModified": "2026-08-19T04:03:12.000Z",
                    "version": 27,
                },
            }
        )

    async with (
        serve_sarp(handler) as base_url,
        SarpClient(SarpClientConfig(token="sarp_test", base_url=base_url, max_retries=0)) as client,
    ):
        cache = await client.get_server_cache_info()

    assert cache == SarpServerCacheInfo.model_validate(
        {
            "exists": True,
            "filename": "server-cache.zip",
            "url": "https://cdn.example.com/server-cache.zip?v=27",
            "sizeBytes": 822_146_470,
            "lastModified": "2026-08-19T04:03:12.000Z",
            "version": 27,
        }
    )
    assert cache.version == 27


async def test_mdc_endpoints_use_documented_paths_filters_and_typed_payloads() -> None:
    seen: list[tuple[str, dict[str, str]]] = []

    async def handler(request: web.Request) -> web.Response:
        seen.append((request.path, dict(request.query)))
        payloads: dict[str, object] = {
            "/api/character/search": [{"id": 44, "name": "Terry_McKenzie"}],
            "/api/character/preview/44": {
                "id": 44,
                "name": "Terry_McKenzie",
                "jailed": 0,
                "level": 99,
                "account_id": 9001,
            },
            "/api/character/preview/44/possessions": {
                "properties": [],
                "vehicles": [
                    {
                        "id": 5,
                        "model": 596,
                        "model_name": "Premier",
                        "plate": "LS-1234",
                    }
                ],
            },
            "/api/police/fines": {
                "items": [
                    {
                        "id": 6,
                        "character_id": 44,
                        "fine": "Conducción temeraria",
                        "cost": 1500,
                        "is_paid": 0,
                    }
                ],
                "total": 1,
                "page": 1,
                "limit": 100,
                "pages": 1,
            },
            "/api/police/warrants": {
                "items": [
                    {
                        "id": 7,
                        "suspect_id": 44,
                        "description": "Detención pendiente",
                        "type": ["arrest"],
                        "status": 1,
                        "charges": [],
                    }
                ],
                "total": 1,
                "page": 1,
                "limit": 25,
                "pages": 1,
            },
            "/api/police/arrests": {
                "items": [{"id": 8, "suspect_id": 44, "charges": []}],
                "total": 1,
                "page": 1,
                "limit": 3,
                "pages": 1,
            },
        }
        return web.json_response(
            {
                "message": "OK",
                "httpCode": 200,
                "source": "test",
                "payload": payloads[request.path],
            }
        )

    async with (
        serve_sarp(handler) as base_url,
        SarpClient(SarpClientConfig(token="sarp_test", base_url=base_url, max_retries=0)) as client,
    ):
        search = await client.search_characters("Terry_McKenzie")
        preview = await client.get_character_preview(44)
        possessions = await client.get_character_possessions(44)
        fines = await client.list_unpaid_fines(44)
        warrants = await client.list_active_warrants(44)
        arrests = await client.list_arrests(44)

    assert search[0].name == "Terry_McKenzie"
    assert preview.jailed == 0
    assert not hasattr(preview, "level")
    assert possessions.vehicles[0].plate == "LS-1234"
    assert fines.items[0].cost == 1500
    assert warrants.items[0].types == ("arrest",)
    assert warrants.items[0].active is True
    assert arrests.total == 1
    assert seen == [
        ("/api/character/search", {"name": "Terry_McKenzie"}),
        ("/api/character/preview/44", {}),
        ("/api/character/preview/44/possessions", {}),
        (
            "/api/police/fines",
            {"character_id": "44", "is_paid": "0", "page": "1", "limit": "100"},
        ),
        (
            "/api/police/warrants",
            {"suspect_id": "44", "status": "1", "page": "1", "limit": "25"},
        ),
        ("/api/police/arrests", {"suspect_id": "44", "page": "1", "limit": "3"}),
    ]


async def test_emergency_call_endpoint_is_typed_and_treats_naive_dates_as_utc() -> None:
    async def handler(request: web.Request) -> web.Response:
        assert request.path == "/api/police/emergency-calls"
        assert request.query == {"page": "2", "limit": "25"}
        return web.json_response(
            {
                "message": "OK",
                "httpCode": 200,
                "source": "pcu",
                "payload": {
                    "items": [
                        {
                            "id": 56221,
                            "character_id": 11040,
                            "character_name": "Andrew McFly",
                            "phone_number": 0,
                            "trace": "Mulholland Drive",
                            "service": "LSPD",
                            "location": "Mulholland Drive",
                            "situation": "Dos sujetos estarían asaltando un local.",
                            "created_at": "2026-08-17 22:27:36",
                        }
                    ],
                    "total": 30,
                    "page": 2,
                    "limit": 25,
                    "pages": 2,
                },
            }
        )

    async with (
        serve_sarp(handler) as base_url,
        SarpClient(SarpClientConfig(token="sarp_test", base_url=base_url, max_retries=0)) as client,
    ):
        page = await client.list_emergency_calls(page=2, limit=25)

    assert page.items[0].id == 56221
    assert page.items[0].created_at.utcoffset().total_seconds() == 0
    assert page.items[0].location == "Mulholland Drive"


async def test_stolen_vehicle_endpoint_uses_filters_and_parses_typed_payload() -> None:
    async def handler(request: web.Request) -> web.Response:
        assert request.path == "/api/police/stolen-vehicles"
        assert request.query == {
            "page": "2",
            "limit": "10",
            "is_active": "1",
            "plate": "ABC123",
            "owner_id": "44",
        }
        return web.json_response(
            {
                "message": "OK",
                "httpCode": 200,
                "source": "pcu",
                "payload": {
                    "items": [
                        {
                            "id": 902,
                            "owner_id": 44,
                            "owner_name": "Terry Davis",
                            "model": 560,
                            "plate": "ABC123",
                            "colors": ["Negro", "Blanco"],
                            "description": "Sultan denunciado como robado.",
                            "is_active": 1,
                            "created_at": "2026-08-21 18:15:00",
                        }
                    ],
                    "total": 11,
                    "page": 2,
                    "limit": 10,
                    "pages": 2,
                },
            }
        )

    async with (
        serve_sarp(handler) as base_url,
        SarpClient(SarpClientConfig(token="sarp_test", base_url=base_url, max_retries=0)) as client,
    ):
        page = await client.list_stolen_vehicles(
            page=2,
            limit=10,
            is_active=True,
            plate=" ABC123 ",
            owner_id=44,
        )

    assert page.total == 11
    assert page.items[0].id == 902
    assert page.items[0].colors == ("Negro", "Blanco")
    assert page.items[0].is_active is True
    assert page.items[0].created_at.utcoffset().total_seconds() == 0


async def test_evidence_detail_endpoint_returns_contents_and_custody() -> None:
    async def handler(request: web.Request) -> web.Response:
        assert request.path == "/api/police/evidences/4385409"
        return web.json_response(
            {
                "message": "OK",
                "httpCode": 200,
                "source": "police-data",
                "payload": {
                    "id": 4385409,
                    "results": [
                        {
                            "id": 23535,
                            "evidence_id": 4385409,
                            "sample_id": 4384680,
                            "police_id": 2869,
                            "police_name": "Deputy Sheriff Lemus Fagan",
                            "police_agency": "Los Santos County Sheriff's Department",
                            "result": "Lata de ECola, cantidad: 10, huellas: Tyler Morgan; ",
                            "created_at": "2026-08-19 22:02:46",
                        }
                    ],
                    "logs": [
                        {
                            "id": 16948,
                            "evidence_id": 4385409,
                            "police_id": 2869,
                            "police_name": "Deputy Sheriff Lemus Fagan",
                            "action": "guarda la bolsa en el almacén 917.",
                            "created_at": "2026-08-16 06:13:26",
                        }
                    ],
                },
            }
        )

    async with (
        serve_sarp(handler) as base_url,
        SarpClient(SarpClientConfig(token="sarp_test", base_url=base_url, max_retries=0)) as client,
    ):
        detail = await client.get_evidence(4_385_409)

    assert detail.id == 4_385_409
    assert detail.results[0].sample_id == 4_384_680
    assert detail.results[0].created_at.tzinfo is not None
    assert detail.logs[0].action.endswith("almacén 917.")


async def test_faction_radio_logs_endpoint_returns_typed_page_and_filters() -> None:
    async def handler(request: web.Request) -> web.Response:
        assert request.path == "/api/my-faction/logs/radio/3"
        assert request.query == {
            "dateFrom": "2026-08-20",
            "dateTo": "2026-08-22",
            "page": "2",
            "limit": "50",
            "search": "Derek Valdez",
            "sortField": "created_at",
            "sortOrder": "asc",
        }
        return web.json_response(
            {
                "message": "OK",
                "httpCode": 200,
                "source": "pcu",
                "payload": {
                    "data": [
                        {
                            "id": 77,
                            "faction": 3,
                            "frequency": 911.0,
                            "character_id": 42,
                            "character_name": "Derek_Valdez",
                            "message": "[CH: SCC, S: 1] Derek Valdez: 10-8",
                            "created_at": "2026-08-22 19:30:00",
                        }
                    ],
                    "total": 51,
                },
            }
        )

    async with (
        serve_sarp(handler) as base_url,
        SarpClient(SarpClientConfig(token="sarp_test", base_url=base_url, max_retries=0)) as client,
    ):
        page = await client.list_faction_radio_logs(
            3,
            date_from=date(2026, 8, 20),
            date_to=date(2026, 8, 22),
            page=2,
            limit=50,
            search=" Derek Valdez ",
            sort_order="asc",
        )

    assert page.total == 51
    assert page.data[0].character_name == "Derek Valdez"
    assert page.data[0].message.endswith("10-8")
    assert page.data[0].created_at.utcoffset().total_seconds() == 0


async def test_faction_detail_endpoint_returns_typed_live_member_states() -> None:
    async def handler(request: web.Request) -> web.Response:
        assert request.path == "/api/my-faction/faction/3"
        return web.json_response(
            {
                "message": "OK",
                "httpCode": 200,
                "source": "pcu",
                "payload": {
                    "factionData": {"id": 3, "name": "Sheriff Department"},
                    "factionMembers": [
                        {
                            "id": 41,
                            "name": "Zachary_Wilcox",
                            "online": 1,
                            "factionduty": 0,
                            "rank": 8,
                            "division": 2,
                            "lastcon": "2026-08-18 12:30:00",
                            "factiontime": 878,
                        },
                        {
                            "id": 42,
                            "name": "Adriel_Beltran",
                            "online": 1,
                            "factionduty": 1,
                            "rank": 7,
                        },
                    ],
                },
            }
        )

    async with (
        serve_sarp(handler) as base_url,
        SarpClient(SarpClientConfig(token="sarp_test", base_url=base_url, max_retries=0)) as client,
    ):
        detail = await client.get_faction_detail(3)

    assert isinstance(detail, SarpFactionDetail)
    assert detail.faction_members[0].online is True
    assert detail.faction_members[0].factionduty is False
    assert detail.faction_members[0].rank == 8
    assert detail.faction_members[0].division == 2
    assert detail.faction_members[0].last_connected_at is not None
    assert detail.faction_members[0].last_connected_at.utcoffset().total_seconds() == 0
    assert detail.faction_members[0].faction_time_minutes == 878
    assert detail.faction_members[1].factionduty is True


async def test_faction_rank_endpoint_returns_the_ordered_typed_catalog() -> None:
    async def handler(request: web.Request) -> web.Response:
        assert request.path == "/api/my-faction/ranks/3"
        return web.json_response(
            {
                "message": "OK",
                "httpCode": 200,
                "source": "pcu",
                "payload": [
                    {
                        "id": 20,
                        "faction": 3,
                        "name": "Employee",
                        "member_tier": 0,
                        "paycheck": 300,
                    },
                    {
                        "id": 21,
                        "faction": 3,
                        "name": "Deputy Sheriff",
                        "member_tier": 0,
                        "paycheck": 725,
                    },
                ],
            }
        )

    async with (
        serve_sarp(handler) as base_url,
        SarpClient(SarpClientConfig(token="sarp_test", base_url=base_url, max_retries=0)) as client,
    ):
        ranks = await client.get_faction_ranks(3)

    assert ranks == [
        SarpFactionRank(
            id=20,
            faction=3,
            name="Employee",
            member_tier=0,
            paycheck=300,
        ),
        SarpFactionRank(
            id=21,
            faction=3,
            name="Deputy Sheriff",
            member_tier=0,
            paycheck=725,
        ),
    ]


async def test_faction_division_endpoint_returns_the_ordered_typed_catalog() -> None:
    async def handler(request: web.Request) -> web.Response:
        assert request.path == "/api/my-faction/divisions/3"
        return web.json_response(
            {
                "message": "OK",
                "httpCode": 200,
                "source": "pcu",
                "payload": [
                    {"id": 5, "faction": 3, "name": "GNT"},
                    {"id": 6, "faction": 3, "name": "DLM"},
                ],
            }
        )

    async with (
        serve_sarp(handler) as base_url,
        SarpClient(SarpClientConfig(token="sarp_test", base_url=base_url, max_retries=0)) as client,
    ):
        divisions = await client.get_faction_divisions(3)

    assert divisions == [
        SarpFactionDivision(id=5, faction=3, name="GNT"),
        SarpFactionDivision(id=6, faction=3, name="DLM"),
    ]


async def test_faction_bank_endpoint_sends_dates_and_returns_typed_movements() -> None:
    async def handler(request: web.Request) -> web.Response:
        assert request.path == "/api/my-faction/logs/bank/3"
        assert dict(request.query) == {
            "dateFrom": "2026-08-18",
            "dateTo": "2026-08-19",
            "page": "2",
            "limit": "500",
            "sortField": "created_at",
            "sortOrder": "desc",
        }
        return web.json_response(
            {
                "message": "OK",
                "httpCode": 200,
                "source": "pcu",
                "payload": {
                    "data": [
                        {
                            "id": 91,
                            "faction_id": 3,
                            "character_id": 44,
                            "character_name": "Charles_Harrington",
                            "concept": "  Depósito de prueba  ",
                            "money": -1000,
                            "created_at": "2026-08-19 08:11:08",
                        }
                    ],
                    "total": 501,
                },
            }
        )

    async with (
        serve_sarp(handler) as base_url,
        SarpClient(SarpClientConfig(token="sarp_test", base_url=base_url, max_retries=0)) as client,
    ):
        page = await client.list_faction_bank_logs(
            3,
            date_from=date(2026, 8, 18),
            date_to=date(2026, 8, 19),
            page=2,
        )

    assert page.total == 501
    assert page.data[0].character_name == "Charles Harrington"
    assert page.data[0].concept == "Depósito de prueba"
    assert page.data[0].amount == -1000
    assert page.data[0].created_at.utcoffset().total_seconds() == 0


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

    async with (
        serve_sarp(handler) as base_url,
        SarpClient(SarpClientConfig(base_url=base_url, max_retries=0)) as client,
    ):
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

    async with (
        serve_sarp(handler) as base_url,
        SarpClient(
            SarpClientConfig(base_url=base_url, max_retries=1),
            sleep=record_sleep,
        ) as client,
    ):
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

    async with (
        serve_sarp(handler) as base_url,
        SarpClient(SarpClientConfig(base_url=base_url, max_retries=3)) as client,
    ):
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
            return web.json_response({"message": "OK", "httpCode": 201, "payload": {"ok": True}})
        return web.json_response({"message": "OK", "httpCode": 200, "payload": {"ok": "not-a-bool"}})

    async with (
        serve_sarp(handler) as base_url,
        SarpClient(SarpClientConfig(base_url=base_url, max_retries=0)) as client,
    ):
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


async def test_arrest_search_and_detail_follow_live_sarp_contract() -> None:
    seen: list[tuple[str, dict[str, str]]] = []

    async def handler(request: web.Request) -> web.Response:
        seen.append((request.path, dict(request.query)))
        if request.path == "/api/my-faction/arrests/3":
            payload = {
                "data": [
                    {
                        "id": 321,
                        "created_at": "2026-08-18T12:00:00",
                        "description": "El sospechoso huyó.",
                        "police_id": 11,
                        "police_name": "Charles_Harrington",
                        "suspect_id": 44,
                        "suspect_name": "Terry_McKenzie",
                    }
                ],
                "total": 1,
            }
        else:
            payload = {
                "id": 321,
                "police_id": 11,
                "police_name": "Charles_Harrington",
                "suspect_id": 44,
                "suspect_name": "Terry_McKenzie",
                "description": "El sospechoso huyó y fue detenido.",
                "created_at": "2026-08-18T12:00:00",
                "updated_at": "2026-08-18T12:30:00Z",
                "charges": [
                    {
                        "charge_id": 91,
                        "code": "TEST-91",
                        "charge": "Evasión",
                        "type": "Felonía",
                        "hours": 4,
                        "fine": 500,
                    }
                ],
            }
        return web.json_response({"message": "OK", "httpCode": 200, "source": "pcu", "payload": payload})

    async with (
        serve_sarp(handler) as base_url,
        SarpClient(SarpClientConfig(token="sarp_test", base_url=base_url, max_retries=0)) as client,
    ):
        search = await client.search_faction_arrests(
            3,
            suspect_name="Terry McKenzie",
            officer_name="Charles Harrington",
            date_from=date(2026, 8, 1),
            date_to=date(2026, 8, 18),
            page=2,
            limit=5,
            sort_field="created_at",
            sort_order="asc",
        )
        detail = await client.get_arrest(321)

    assert search.total == 1
    assert search.data[0].created_at.tzinfo is not None
    assert detail.charges[0].charge == "Evasión"
    assert seen == [
        (
            "/api/my-faction/arrests/3",
            {
                "page": "2",
                "limit": "5",
                "dateFrom": "2026-08-01",
                "dateTo": "2026-08-18",
                "suspectSearch": "Terry McKenzie",
                "agentSearch": "Charles Harrington",
                "sortField": "created_at",
                "sortOrder": "1",
            },
        ),
        ("/api/police/arrests/321", {}),
    ]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"limit": 51},
        {"date_from": date(2026, 8, 19), "date_to": date(2026, 8, 18)},
        {"sort_field": "created_at;drop"},
        {"sort_order": "sideways"},
        {"suspect_name": "   "},
    ],
)
async def test_arrest_search_rejects_invalid_filters(kwargs: dict[str, object]) -> None:
    async def handler(_request: web.Request) -> web.Response:
        raise AssertionError("invalid filters must fail before transport")

    async with (
        serve_sarp(handler) as base_url,
        SarpClient(SarpClientConfig(base_url=base_url, max_retries=0)) as client,
    ):
        with pytest.raises(ValueError):
            await client.search_faction_arrests(3, **kwargs)  # type: ignore[arg-type]

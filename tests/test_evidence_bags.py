from __future__ import annotations

import json
from datetime import UTC
from types import SimpleNamespace

import discord
from agents.tool_context import ToolContext
from discord.commands import Option
from discord.ext import commands

from arrest_report_agent.application.ports import (
    EvidenceBagCustodyEvent,
    EvidenceBagDetail,
    EvidenceBagItem,
    EvidenceBagLookupResult,
)
from arrest_report_agent.application.prompt import build_agent_instructions
from arrest_report_agent.application.service_context import ReviewRunContext
from arrest_report_agent.application.tools import build_agent_tools
from arrest_report_agent.domain import AgentAccessLevel, AgentActivity, InteractionMode, ReviewCase
from arrest_report_agent.evidence_bags import (
    EvidenceBagCog,
    EvidenceBagService,
    build_evidence_bag_embed,
)
from arrest_report_agent.evidence_bags.service import _strip_samp_color_tags
from arrest_report_agent.infrastructure.sarp import (
    SarpEvidenceDetail,
    SarpRateLimit,
    SarpRateLimitError,
)
from arrest_report_agent.knowledge import ChargeCatalog, ReportGuide


def evidence_payload(evidence_id: int = 4_385_409) -> SarpEvidenceDetail:
    return SarpEvidenceDetail.model_validate(
        {
            "id": evidence_id,
            "results": [
                {
                    "id": 23535,
                    "evidence_id": evidence_id,
                    "sample_id": 4384680,
                    "police_id": 2869,
                    "police_name": "{B8C0CB}Deputy Sheriff Lemus Fagan{A9C4E4}",
                    "police_agency": "{A9C4E4}Los Santos County Sheriff's Department",
                    "result": "{B8C0CB}Lata de ECola, cantidad: 10, huellas: Tyler Morgan; ",
                    "created_at": "2026-08-19 22:02:46",
                },
                {
                    "id": 23534,
                    "evidence_id": evidence_id,
                    "sample_id": 4334920,
                    "police_id": 2869,
                    "police_name": "Deputy Sheriff Lemus Fagan",
                    "police_agency": "Los Santos County Sheriff's Department",
                    "result": "{B8C0CBFF}Radio UHF, cantidad: 4032, huellas: Tyler Morgan; ",
                    "created_at": "2026-08-19 22:02:46",
                },
            ],
            "logs": [
                {
                    "id": 16948,
                    "evidence_id": evidence_id,
                    "police_id": 2869,
                    "police_name": "Deputy Sheriff Lemus Fagan",
                    "action": f"{{D0AD9F}}guarda la bolsa de evidencias #{evidence_id} en el almacén 917.",
                    "created_at": "2026-08-16 06:13:26",
                }
            ],
        }
    )


class FakeEvidenceClient:
    def __init__(self, result: SarpEvidenceDetail | Exception) -> None:
        self.result = result
        self.calls: list[int] = []

    async def get_evidence(self, evidence_id: int) -> SarpEvidenceDetail:
        self.calls.append(evidence_id)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeEvidenceLookup:
    def __init__(self, result: EvidenceBagLookupResult) -> None:
        self.result = result
        self.calls: list[int] = []

    async def get(self, evidence_id: int) -> EvidenceBagLookupResult:
        self.calls.append(evidence_id)
        return self.result


class FakePublisher:
    async def mark_review_started(self) -> None: ...

    async def publish(self, _publication: object) -> None: ...

    async def publish_failure(self, _message: str) -> None: ...


class ActivityRecorder:
    def __init__(self) -> None:
        self.activities: list[AgentActivity] = []

    async def transition(self, activity: AgentActivity) -> None:
        self.activities.append(activity)


def _member(user_id: int, role_ids: tuple[int, ...]) -> discord.Member:
    roles = {role_id: SimpleNamespace(id=role_id) for role_id in role_ids}
    guild = SimpleNamespace(id=1, get_role=roles.get, default_role=None)
    state = SimpleNamespace(
        store_user=lambda data: SimpleNamespace(
            id=int(data["id"]),
            global_name=None,
            name="Charles Harrington",
        )
    )
    return discord.Member(
        data={"user": {"id": str(user_id)}, "roles": [str(role_id) for role_id in role_ids]},
        guild=guild,
        state=state,
    )


def context(*, access_level: AgentAccessLevel = AgentAccessLevel.FULL) -> ReviewRunContext:
    return ReviewRunContext(
        case=ReviewCase(
            guild_id=11111111111111111,
            parent_channel_id=22222222222222222,
            channel_id=33333333333333333,
            thread_id=33333333333333333,
            root_message_id=33333333333333333,
            trigger_message_id=55555555555555555,
            requester_user_id=66666666666666666,
            interaction_mode=InteractionMode.THREAD_FOLLOW_UP,
            messages=[],
            attachments=[],
        ),
        publisher=FakePublisher(),  # type: ignore[arg-type]
        attempt=1,
        access_level=access_level,
    )


def tools(lookup: FakeEvidenceLookup, access_level: AgentAccessLevel) -> dict[str, object]:
    return {
        tool.name: tool
        for tool in build_agent_tools(
            ChargeCatalog.load_default(),
            ReportGuide.load_default(),
            access_level=access_level,
            evidence_bag_lookup=lookup,
        )
    }


def test_samp_color_sanitizer_preserves_plain_text_and_removes_rgb_and_rgba_tags() -> None:
    assert _strip_samp_color_tags("Casquillo 9x19 sin coincidencias.") == "Casquillo 9x19 sin coincidencias."
    assert (
        _strip_samp_color_tags("{B8C0CB}[Balística: 4254915]{A9C4E4} Casquillo 9x19")
        == "[Balística: 4254915] Casquillo 9x19"
    )
    assert _strip_samp_color_tags("{B8C0CBFF}Muestra de sangre") == "Muestra de sangre"


async def test_service_normalizes_one_live_evidence_bag_without_caching() -> None:
    client = FakeEvidenceClient(evidence_payload())
    service = EvidenceBagService(client)

    first = await service.get(4_385_409)
    second = await service.get(4_385_409)

    assert first.detail is not None
    assert first.detail.evidence_id == 4_385_409
    assert [item.result for item in first.detail.items] == [
        "Lata de ECola, cantidad: 10, huellas: Tyler Morgan;",
        "Radio UHF, cantidad: 4032, huellas: Tyler Morgan;",
    ]
    assert first.detail.items[0].examined_at.tzinfo is UTC
    assert first.detail.custody[0].action.endswith("almacén 917.")
    assert first.detail.items[0].officer_name == "Deputy Sheriff Lemus Fagan"
    assert first.detail.items[0].agency == "Los Santos County Sheriff's Department"
    assert "{" not in first.detail.custody[0].action
    assert second == first
    assert client.calls == [4_385_409, 4_385_409]


async def test_service_exposes_safe_not_found_rate_limit_and_empty_states() -> None:
    rate_limit = SarpRateLimitError(
        status_code=429,
        message="too_many_requests",
        source="police-data",
        request_id="request-1",
        rate_limit=SarpRateLimit(retry_after_seconds=12),
    )
    limited = await EvidenceBagService(FakeEvidenceClient(rate_limit)).get(7)
    empty = await EvidenceBagService(FakeEvidenceClient(SarpEvidenceDetail(id=7))).get(7)

    assert limited.error == "rate_limited"
    assert limited.retry_after_seconds == 12
    assert empty.error is None
    assert empty.detail is not None
    assert empty.detail.items == ()


async def test_full_agent_tool_returns_flexible_projection_and_uses_bag_presence() -> None:
    detail = await EvidenceBagService(FakeEvidenceClient(evidence_payload())).get(4_385_409)
    lookup = FakeEvidenceLookup(detail)
    selected = tools(lookup, AgentAccessLevel.FULL)
    run_context = context()
    activity = ActivityRecorder()
    run_context.activity = activity
    arguments = json.dumps(
        {
            "evidence_id": 4_385_409,
            "include_custody": False,
            "include_analysis_metadata": False,
        }
    )

    result = await selected["get_evidence_bag"].on_invoke_tool(  # type: ignore[attr-defined]
        ToolContext(
            context=run_context,
            tool_name="get_evidence_bag",
            tool_call_id="evidence",
            tool_arguments=arguments,
        ),
        arguments,
    )

    assert result == {
        "status": "analyzed",
        "evidence_id": 4_385_409,
        "items": [
            {"result": "Lata de ECola, cantidad: 10, huellas: Tyler Morgan;"},
            {"result": "Radio UHF, cantidad: 4032, huellas: Tyler Morgan;"},
        ],
    }
    assert lookup.calls == [4_385_409]
    assert activity.activities == [AgentActivity.EVIDENCE_BAG]


async def test_evidence_tool_distinguishes_existing_unanalyzed_bag_from_missing_id() -> None:
    existing = await EvidenceBagService(FakeEvidenceClient(SarpEvidenceDetail(id=7))).get(7)
    existing_tools = tools(FakeEvidenceLookup(existing), AgentAccessLevel.FULL)
    existing_context = context()
    arguments = json.dumps({"evidence_id": 7, "include_custody": False})

    existing_result = await existing_tools["get_evidence_bag"].on_invoke_tool(  # type: ignore[attr-defined]
        ToolContext(
            context=existing_context,
            tool_name="get_evidence_bag",
            tool_call_id="existing-unanalysed",
            tool_arguments=arguments,
        ),
        arguments,
    )

    missing_tools = tools(
        FakeEvidenceLookup(EvidenceBagLookupResult(detail=None, error="not_found")),
        AgentAccessLevel.FULL,
    )
    missing_result = await missing_tools["get_evidence_bag"].on_invoke_tool(  # type: ignore[attr-defined]
        ToolContext(
            context=context(),
            tool_name="get_evidence_bag",
            tool_call_id="missing",
            tool_arguments=arguments,
        ),
        arguments,
    )

    assert existing_result == {
        "status": "exists_unanalyzed",
        "evidence_id": 7,
        "items": [],
    }
    assert missing_result == {
        "status": "not_found",
        "evidence_id": 7,
    }


async def test_evidence_tool_is_available_to_both_access_levels_and_bounded_per_run() -> None:
    result = await EvidenceBagService(FakeEvidenceClient(evidence_payload())).get(4_385_409)
    lookup = FakeEvidenceLookup(result)
    full = tools(lookup, AgentAccessLevel.FULL)
    owner = tools(lookup, AgentAccessLevel.REPORT_OWNER)
    run_context = context()
    arguments = json.dumps({"evidence_id": 4_385_409})

    assert "get_evidence_bag" in full
    assert "get_evidence_bag" in owner
    invocations = []
    for index in range(4):
        invocations.append(
            await full["get_evidence_bag"].on_invoke_tool(  # type: ignore[attr-defined]
                ToolContext(
                    context=run_context,
                    tool_name="get_evidence_bag",
                    tool_call_id=f"evidence-{index}",
                    tool_arguments=arguments,
                ),
                arguments,
            )
        )

    assert [item["status"] for item in invocations] == [
        "analyzed",
        "analyzed",
        "analyzed",
        "run_limit_reached",
    ]
    assert lookup.calls == [4_385_409, 4_385_409, 4_385_409]


def test_evidence_instructions_are_exposed_to_both_agents(persona) -> None:
    full = build_agent_instructions(persona, evidence_lookup_enabled=True)
    owner = build_agent_instructions(
        persona,
        AgentAccessLevel.REPORT_OWNER,
        evidence_lookup_enabled=True,
    )

    assert "<sarp_evidence_bags>" in full
    assert "get_evidence_bag" in full
    assert "como máximo un emoji" in full
    assert "no agregues ninguno" in full
    assert "exists_unanalyzed" in full
    assert "not_found" in full
    assert "<sarp_evidence_bags>" in owner
    assert "get_evidence_bag" in owner


def test_evidence_tool_description_declares_every_return_status() -> None:
    result = EvidenceBagLookupResult(detail=None, error="not_found")
    tool = tools(FakeEvidenceLookup(result), AgentAccessLevel.FULL)["get_evidence_bag"]

    assert tool.description is not None  # type: ignore[attr-defined]
    for status in (
        "analyzed",
        "exists_unanalyzed",
        "not_found",
        "rate_limited",
        "unavailable",
        "run_limit_reached",
    ):
        assert status in tool.description  # type: ignore[attr-defined]


async def test_command_exposes_evidence_id_as_a_real_integer_option() -> None:
    bot = commands.Bot(intents=discord.Intents.none())
    bot.add_cog(
        EvidenceBagCog(
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(),
        )
    )
    command = next(command for command in bot.pending_application_commands if command.name == "ver-evidencia")
    assert [option.name for option in command.options] == ["bolsa"]
    option = command.options[0]

    assert isinstance(option, Option)
    assert option._raw_type is int  # noqa: SLF001 - regression for Pycord's invoke parser
    assert option.input_type is discord.SlashCommandOptionType.integer
    assert option.min_value == 1
    await bot.close()


def test_public_embed_preserves_results_and_custody_without_internal_ids() -> None:
    payload = evidence_payload()
    bag = EvidenceBagDetail(
        evidence_id=payload.id,
        items=tuple(
            EvidenceBagItem(
                analysis_id=item.id,
                sample_id=item.sample_id,
                result=item.result,
                officer_name=item.police_name,
                agency=item.police_agency,
                examined_at=item.created_at,
            )
            for item in payload.results
        ),
        custody=tuple(
            EvidenceBagCustodyEvent(
                event_id=entry.id,
                action=entry.action,
                officer_name=entry.police_name,
                created_at=entry.created_at,
            )
            for entry in payload.logs
        ),
    )

    embed = build_evidence_bag_embed(bag, requested_by="Charles Harrington")

    assert embed.title == "📦 Bolsa de evidencias #4385409"
    assert "Lata de ECola" in embed.fields[0].value
    assert "Deputy Sheriff Lemus Fagan" in embed.fields[1].value
    assert "almacén 917" in embed.fields[1].value
    assert "23535" not in str(embed.to_dict())
    assert embed.footer.text == "Consulta realizada por Charles Harrington"


def test_public_embed_explains_existing_bag_without_laboratory_results() -> None:
    embed = build_evidence_bag_embed(
        EvidenceBagDetail(evidence_id=7, items=(), custody=()),
    )

    assert "La bolsa existe" in embed.fields[0].value
    assert "no presenta resultados de laboratorio" in embed.fields[0].value
    assert "vacía" not in embed.fields[0].value


async def test_command_rejects_non_operational_member_before_lookup(persona) -> None:
    responses: list[dict[str, object]] = []
    member = _member(11, (999,))

    async def cached(_guild_id: int):
        return SimpleNamespace(
            settings=SimpleNamespace(
                operational_staff_role_ids=(200,),
                arrest_report_lookup_faction_id=3,
            )
        )

    async def respond(**kwargs: object) -> None:
        responses.append(kwargs)

    result = await EvidenceBagService(FakeEvidenceClient(evidence_payload())).get(4_385_409)
    lookup = FakeEvidenceLookup(result)
    cog = EvidenceBagCog(SimpleNamespace(cached=cached), lookup, persona)
    command_context = SimpleNamespace(guild=member.guild, author=member, respond=respond)

    await EvidenceBagCog.view_evidence.callback(  # type: ignore[arg-type]
        cog,
        command_context,
        4_385_409,
    )

    assert responses[0]["ephemeral"] is True
    assert lookup.calls == []


async def test_command_publishes_shared_lookup_for_operational_member(persona) -> None:
    deferred: list[dict[str, object]] = []
    followups: list[dict[str, object]] = []
    member = _member(11, (200,))

    async def cached(_guild_id: int):
        return SimpleNamespace(
            settings=SimpleNamespace(
                operational_staff_role_ids=(200,),
                arrest_report_lookup_faction_id=3,
            )
        )

    async def defer(**kwargs: object) -> None:
        deferred.append(kwargs)

    async def send(**kwargs: object) -> None:
        followups.append(kwargs)

    result = await EvidenceBagService(FakeEvidenceClient(evidence_payload())).get(4_385_409)
    lookup = FakeEvidenceLookup(result)
    cog = EvidenceBagCog(SimpleNamespace(cached=cached), lookup, persona)
    command_context = SimpleNamespace(
        guild=member.guild,
        author=member,
        channel_id=123,
        defer=defer,
        followup=SimpleNamespace(send=send),
    )

    await EvidenceBagCog.view_evidence.callback(  # type: ignore[arg-type]
        cog,
        command_context,
        4_385_409,
    )

    assert deferred == [{"ephemeral": False}]
    assert followups[0]["ephemeral"] is False
    assert followups[0]["embed"].title == "📦 Bolsa de evidencias #4385409"
    assert lookup.calls == [4_385_409]

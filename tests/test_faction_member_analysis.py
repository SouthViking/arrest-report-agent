from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import discord
import pytest

from arrest_report_agent.config import (
    AccessPolicyKey,
    AccessPolicyMode,
    AccessPolicySnapshot,
    AppSettings,
    FactionMemberAnalysisOptions,
    GuildConfigSnapshot,
    GuildFeatureSnapshot,
    GuildRuntimeSettings,
)
from arrest_report_agent.faction_directory import FactionActivityLevel
from arrest_report_agent.faction_member_analysis import (
    FactionDivisionCount,
    FactionMemberAnalysisCog,
    FactionMemberAnalysisModal,
    FactionMemberAnalysisService,
    FactionMemberAnalysisVisibility,
    build_faction_member_analysis_embed,
)
from arrest_report_agent.infrastructure.sarp import SarpFactionDivision, SarpFactionMember


def _member(user_id: int, role_ids: tuple[int, ...]) -> discord.Member:
    roles = {role_id: SimpleNamespace(id=role_id) for role_id in role_ids}
    guild = SimpleNamespace(id=1, get_role=roles.get, default_role=None)
    state = SimpleNamespace(store_user=lambda data: SimpleNamespace(id=int(data["id"])))
    return discord.Member(
        data={"user": {"id": str(user_id)}, "roles": [str(role_id) for role_id in role_ids]},
        guild=guild,
        state=state,
    )


def _options() -> FactionMemberAnalysisOptions:
    return FactionMemberAnalysisOptions(
        faction_id=3,
        base_divisions=("GNT", "DLM"),
        inactivity_warning_days=14,
        inactivity_critical_days=30,
        inactivity_warning_percentage=20,
        inactivity_critical_percentage=30,
    )


def _runtime_settings(settings: AppSettings) -> GuildRuntimeSettings:
    snapshot = GuildConfigSnapshot.model_construct(
        guild_id=1,
        policies={
            AccessPolicyKey.SUPERVISOR_ROLES: AccessPolicySnapshot(
                key=AccessPolicyKey.SUPERVISOR_ROLES,
                subject_type="role",
                mode=AccessPolicyMode.RESTRICTED,
                ids=(200,),
            )
        },
        features={
            "faction_member_analysis": GuildFeatureSnapshot(
                key="faction_member_analysis",
                enabled=True,
                schema_version=1,
                payload=_options().model_dump(mode="json"),
            )
        },
    )
    return GuildRuntimeSettings(settings, snapshot)


def _roster(now: datetime) -> tuple[SarpFactionMember, ...]:
    ages = (1, 5, 10, 16, 31)
    members = [
        SarpFactionMember(
            id=index,
            name=f"Member_{index}",
            online=index == 1,
            factionduty=index in {1, 4},
            rank=1,
            division=index,
            lastcon=now - timedelta(days=age),
        )
        for index, age in enumerate(ages, start=1)
    ]
    members.extend(
        (
            SarpFactionMember(
                id=6,
                name="No_Lastcon",
                online=False,
                factionduty=False,
                rank=1,
                division=0,
            ),
            SarpFactionMember(
                id=7,
                name="Invalid_Division",
                online=False,
                factionduty=False,
                rank=1,
                division=99,
                lastcon=now - timedelta(days=2),
            ),
        )
    )
    return tuple(members)


def _divisions() -> tuple[SarpFactionDivision, ...]:
    return tuple(
        SarpFactionDivision(id=100 + index, faction=3, name=name)
        for index, name in enumerate(("GNT", "DLM", "IAB", "SEB", "OSSB"), start=1)
    )


async def _analysis(now: datetime):
    calls = {"roster": 0, "divisions": 0}

    async def roster(faction_id: int):
        assert faction_id == 3
        calls["roster"] += 1
        return _roster(now)

    async def divisions(faction_id: int):
        assert faction_id == 3
        calls["divisions"] += 1
        return _divisions()

    service = FactionMemberAnalysisService(
        SimpleNamespace(roster=roster, divisions=divisions)  # type: ignore[arg-type]
    )
    return await service.analyze(_options(), now=now), calls


async def test_analysis_uses_ordinal_divisions_and_deterministic_activity_bands() -> None:
    now = datetime(2026, 8, 24, 12, tzinfo=UTC)
    snapshot, calls = await _analysis(now)

    assert calls == {"roster": 1, "divisions": 1}
    assert snapshot.total_members == 7
    assert snapshot.connected_members == 1
    assert snapshot.on_duty_members == 1
    assert snapshot.activity_count(FactionActivityLevel.VERY_ACTIVE) == 2
    assert snapshot.activity_count(FactionActivityLevel.ACTIVE) == 1
    assert snapshot.activity_count(FactionActivityLevel.REGULAR) == 1
    assert snapshot.activity_count(FactionActivityLevel.AT_RISK) == 1
    assert snapshot.activity_count(FactionActivityLevel.INACTIVE) == 1
    assert snapshot.activity_count(FactionActivityLevel.UNKNOWN) == 1
    assert [(item.name, item.count) for item in snapshot.divisions] == [
        ("GNT", 1),
        ("DLM", 1),
        ("IAB", 1),
        ("SEB", 1),
        ("OSSB", 1),
    ]
    assert snapshot.base_division_count == 2
    assert snapshot.specialty_count == 3
    assert snapshot.unassigned_divisions == 1
    assert snapshot.invalid_divisions == 1
    assert snapshot.over_warning_days == 2
    assert snapshot.over_critical_days == 1
    assert snapshot.unknown_activity == 1
    assert snapshot.inconsistent_duty_state == 1
    assert snapshot.is_critical is True


async def test_analysis_embed_is_compact_and_surfaces_attention(persona) -> None:
    snapshot, _calls = await _analysis(datetime(2026, 8, 24, 12, tzinfo=UTC))
    snapshot = replace(
        snapshot,
        divisions=(
            FactionDivisionCount("GNT", 2, True),
            FactionDivisionCount("DLM", 1, True),
            FactionDivisionCount("IAB", 1, False),
            FactionDivisionCount("SEB", 2, False),
            FactionDivisionCount("OSSB", 0, False),
        ),
    )

    embed = build_faction_member_analysis_embed(
        snapshot,
        persona.messages.interface.faction_member_analysis,
    )

    assert embed.title == "👥 Análisis de miembros"
    assert [(field.name, field.value) for field in embed.fields[:3]] == [
        ("👥 Miembros", "7"),
        ("🟢 Conectados", "1"),
        ("🚓 En servicio", "1"),
    ]
    activity = next(field for field in embed.fields if field.name.startswith("📈"))
    assert "🟢 **Muy activo:** 2 · 28,6%" in activity.value
    distribution = next(field for field in embed.fields if field.name == "🏢 Distribución")
    assert distribution.value.splitlines() == [
        "**Especialidades:** 3",
        "**GNT:** 2",
        "**DLM:** 1",
        "**Sin asignación:** 1",
    ]
    specialties = next(field for field in embed.fields if field.name == "🧩 Especialidades")
    assert specialties.value.splitlines() == [
        "**SEB:** 2",
        "**IAB:** 1",
    ]
    attention = next(field for field in embed.fields if field.name == "⚠️ Atención")
    assert "2 miembros superan los 14 días" in attention.value
    assert "umbral de atención: 20%" in attention.value
    assert "1 miembro supera los 30 días" in attention.value
    assert "1 miembro no tiene división" in attention.value
    assert "1 miembro figura en servicio pero desconectado" in attention.value


async def test_command_uses_supervisor_whitelist_and_opens_public_by_default_modal(
    persona,
    settings: AppSettings,
) -> None:
    denied_responses: list[dict[str, object]] = []
    allowed_modals: list[FactionMemberAnalysisModal] = []
    denied = _member(11, (999,))
    allowed = _member(12, (200,))

    async def cached(_guild_id: int):
        return SimpleNamespace(
            settings=_runtime_settings(settings),
            persona=SimpleNamespace(profile=persona),
        )

    async def respond(**kwargs: object) -> None:
        denied_responses.append(kwargs)

    async def send_modal(modal: FactionMemberAnalysisModal) -> None:
        allowed_modals.append(modal)

    cog = FactionMemberAnalysisCog(SimpleNamespace(cached=cached), SimpleNamespace(), persona)
    await FactionMemberAnalysisCog.member_activity.callback(  # type: ignore[arg-type]
        cog,
        SimpleNamespace(guild=denied.guild, author=denied, respond=respond),
    )
    await FactionMemberAnalysisCog.member_activity.callback(  # type: ignore[arg-type]
        cog,
        SimpleNamespace(guild=allowed.guild, author=allowed, send_modal=send_modal),
    )

    assert denied_responses[0]["ephemeral"] is True
    assert denied_responses[0]["embed"].description == (
        persona.messages.interface.faction_member_analysis.unauthorized
    )
    assert len(allowed_modals) == 1
    assert allowed_modals[0].publish_for_everyone.default is True


async def test_modal_checkbox_maps_to_public_and_private_visibility() -> None:
    captured: list[FactionMemberAnalysisVisibility] = []

    async def publish(_interaction: object, **kwargs: object) -> None:
        captured.append(FactionMemberAnalysisVisibility(str(kwargs["visibility"])))

    modal = FactionMemberAnalysisModal(  # type: ignore[arg-type]
        SimpleNamespace(publish=publish),
        requester_id=10,
    )
    modal.publish_for_everyone.refresh_state({"value": True})
    await modal.callback(SimpleNamespace())  # type: ignore[arg-type]
    modal.publish_for_everyone.refresh_state({"value": False})
    await modal.callback(SimpleNamespace())  # type: ignore[arg-type]

    assert captured == [
        FactionMemberAnalysisVisibility.PUBLIC,
        FactionMemberAnalysisVisibility.PRIVATE,
    ]


@pytest.mark.parametrize(
    ("visibility", "ephemeral"),
    (
        (FactionMemberAnalysisVisibility.PUBLIC, False),
        (FactionMemberAnalysisVisibility.PRIVATE, True),
    ),
)
async def test_analysis_visibility_controls_defer_and_followup(
    persona,
    settings: AppSettings,
    visibility: FactionMemberAnalysisVisibility,
    ephemeral: bool,
) -> None:
    member = _member(11, (200,))
    deferred: list[dict[str, object]] = []
    followups: list[dict[str, object]] = []
    snapshot, _calls = await _analysis(datetime(2026, 8, 24, 12, tzinfo=UTC))

    async def cached(_guild_id: int):
        return SimpleNamespace(
            settings=_runtime_settings(settings),
            persona=SimpleNamespace(profile=persona),
        )

    async def defer(**kwargs: object) -> None:
        deferred.append(kwargs)

    async def send(**kwargs: object) -> None:
        followups.append(kwargs)

    async def analyze(_options: object):
        return snapshot

    cog = FactionMemberAnalysisCog(
        SimpleNamespace(cached=cached),
        SimpleNamespace(analyze=analyze),
        persona,
    )
    interaction = SimpleNamespace(
        guild=member.guild,
        channel=SimpleNamespace(id=22),
        user=member,
        response=SimpleNamespace(defer=defer),
        followup=SimpleNamespace(send=send),
    )

    await cog.publish(
        interaction,
        requester_id=member.id,
        visibility=visibility,
    )

    assert deferred == [{"ephemeral": ephemeral}]
    assert followups[0]["ephemeral"] is ephemeral
    assert followups[0]["allowed_mentions"].everyone is False

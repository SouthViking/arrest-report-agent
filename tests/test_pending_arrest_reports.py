from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import discord
import pytest

from arrest_report_agent.domain import ReviewVerdict
from arrest_report_agent.infrastructure.sarp import (
    SarpFactionArrestSearchPayload,
    SarpFactionArrestSummary,
)
from arrest_report_agent.pending_arrest_reports import (
    PendingArrestReport,
    PendingArrestReportCog,
    PendingArrestReportModal,
    PendingArrestReportScan,
    PendingArrestReportService,
    PendingArrestReviewRepository,
    PendingReportReason,
    PendingReportScanTooLargeError,
    PendingReportStatus,
    PendingReportVisibility,
    PublishedArrestReview,
    UnapprovedArrestReport,
    build_pending_arrest_reports_embed,
    status_for_age,
    unfinished_reason,
)
from arrest_report_agent.pending_arrest_reports.discord import (
    PendingReportPeriod,
    _resolve_date_range,
)


def _summary(
    report_id: int,
    *,
    description: str,
    created_at: datetime,
) -> SarpFactionArrestSummary:
    return SarpFactionArrestSummary(
        id=report_id,
        police_id=100 + report_id,
        police_name=f"Officer_{report_id}",
        suspect_id=200 + report_id,
        suspect_name=f"Suspect_{report_id}",
        description=description,
        created_at=created_at,
    )


def _member(user_id: int, role_ids: tuple[int, ...]) -> discord.Member:
    roles = {role_id: SimpleNamespace(id=role_id) for role_id in role_ids}
    guild = SimpleNamespace(id=1, get_role=roles.get, default_role=None)
    state = SimpleNamespace(store_user=lambda data: SimpleNamespace(id=int(data["id"])))
    return discord.Member(
        data={"user": {"id": str(user_id)}, "roles": [str(role_id) for role_id in role_ids]},
        guild=guild,
        state=state,
    )


@pytest.mark.parametrize(
    "description",
    (
        "Yo, Walter Vigil, procedí a detener al sospechoso tras observar los hechos.",
        "  yo Walter Vigil mantuve al sospechoso bajo custodia.",
        "YO, Walter Vigil informa lo ocurrido.",
        "La unidad observó los hechos y continuó con una narrativa extensa " * 8,
    ),
)
def test_finished_narratives_are_not_flagged(description: str) -> None:
    assert unfinished_reason(description) is None


@pytest.mark.parametrize(
    ("description", "reason"),
    (
        ("", PendingReportReason.EMPTY),
        ("rellenar", PendingReportReason.PLACEHOLDER),
        ("Todo...", PendingReportReason.PLACEHOLDER),
        ("aaaaaaa", PendingReportReason.REPEATED_CONTENT),
        ("línea pendiente\nlínea pendiente", PendingReportReason.REPEATED_CONTENT),
        ("Se detuvo al sujeto.", PendingReportReason.SHORT_NONSTANDARD),
    ),
)
def test_unfinished_narratives_are_classified(
    description: str,
    reason: PendingReportReason,
) -> None:
    assert unfinished_reason(description) is reason


def test_age_status_boundaries_are_deterministic() -> None:
    now = datetime(2026, 8, 18, 12, tzinfo=UTC)

    assert status_for_age(now - timedelta(hours=17, minutes=59), now=now) is (
        PendingReportStatus.ON_TIME
    )
    assert status_for_age(now - timedelta(hours=18), now=now) is (
        PendingReportStatus.NEAR_DEADLINE
    )
    assert status_for_age(now - timedelta(hours=24), now=now) is (
        PendingReportStatus.OVER_NORMAL_LIMIT
    )
    assert status_for_age(now - timedelta(hours=48), now=now) is (
        PendingReportStatus.OVER_MAXIMUM_LIMIT
    )


@pytest.mark.asyncio
async def test_service_paginates_classifies_and_orders_oldest_first() -> None:
    now = datetime(2026, 8, 18, 12, tzinfo=UTC)
    reports = [
        _summary(
            index,
            description=(
                "pendiente"
                if index in {1, 51}
                else "Yo, Officer Example, redacté un informe completo."
            ),
            created_at=now - timedelta(hours=index),
        )
        for index in range(1, 52)
    ]
    calls: list[dict[str, object]] = []

    async def search(_faction_id: int, **kwargs: object) -> SarpFactionArrestSearchPayload:
        calls.append(kwargs)
        page = int(kwargs["page"])
        start = (page - 1) * 50
        return SarpFactionArrestSearchPayload(data=reports[start : start + 50], total=51)

    service = PendingArrestReportService(
        SimpleNamespace(search_faction_arrests=search)
    )
    result = await service.scan(
        faction_id=3,
        date_from=date(2026, 8, 1),
        date_to=date(2026, 8, 18),
        now=now,
    )

    assert [report.report_id for report in result.pending_reports] == [51, 1]
    assert result.total_reports == 51
    assert len(calls) == 2
    assert calls[0]["date_to"] == date(2026, 8, 19)
    assert calls[0]["limit"] == 50


@pytest.mark.asyncio
async def test_service_lists_reports_without_an_approved_formal_review() -> None:
    now = datetime(2026, 8, 18, 12, tzinfo=UTC)
    reports = [
        _summary(
            index,
            description="Yo, Officer Example, redacté un informe completo.",
            created_at=now - timedelta(hours=index),
        )
        for index in range(1, 5)
    ]
    lookup_calls: list[dict[str, object]] = []

    async def search(_faction_id: int, **_kwargs: object) -> SarpFactionArrestSearchPayload:
        return SarpFactionArrestSearchPayload(data=reports, total=len(reports))

    async def latest_published_full_reviews(**kwargs: object):
        lookup_calls.append(kwargs)
        return {
            1: PublishedArrestReview(
                report_id=1,
                verdict=ReviewVerdict.APPROVED,
                guild_id=99,
                parent_channel_id=100,
                root_message_id=1001,
                published_at=now,
            ),
            2: PublishedArrestReview(
                report_id=2,
                verdict=ReviewVerdict.CORRECTIONS_REQUIRED,
                guild_id=99,
                parent_channel_id=100,
                root_message_id=1002,
                published_at=now,
            ),
            3: PublishedArrestReview(
                report_id=3,
                verdict=ReviewVerdict.INCONCLUSIVE,
                guild_id=99,
                parent_channel_id=100,
                root_message_id=1003,
                published_at=now,
            ),
        }

    service = PendingArrestReportService(
        SimpleNamespace(search_faction_arrests=search),
        SimpleNamespace(latest_published_full_reviews=latest_published_full_reviews),
    )

    result = await service.scan(
        faction_id=3,
        date_from=date(2026, 8, 1),
        date_to=date(2026, 8, 18),
        now=now,
        guild_id=99,
        report_channel_ids=frozenset({100, 200}),
    )

    assert [report.report_id for report in result.unapproved_reports] == [4, 3, 2]
    assert result.unapproved_reports[0].review_verdict is None
    assert result.unapproved_reports[1].review_verdict is ReviewVerdict.INCONCLUSIVE
    assert result.unapproved_reports[2].review_url.endswith("/99/100/1002")
    assert lookup_calls == [
        {
            "guild_id": 99,
            "report_ids": (1, 2, 3, 4),
            "channel_ids": frozenset({100, 200}),
        }
    ]


@pytest.mark.asyncio
async def test_review_repository_uses_latest_published_full_review_per_report() -> None:
    now = datetime(2026, 8, 18, 12, tzinfo=UTC)
    statements: list[object] = []

    async def fetch_all(statement: object):
        statements.append(statement)
        return [
            {
                "report_id": 10,
                "verdict": ReviewVerdict.CORRECTIONS_REQUIRED.value,
                "guild_id": 99,
                "parent_channel_id": 100,
                "root_message_id": 1002,
                "published_at": now,
            },
            {
                "report_id": 10,
                "verdict": ReviewVerdict.APPROVED.value,
                "guild_id": 99,
                "parent_channel_id": 100,
                "root_message_id": 1001,
                "published_at": now - timedelta(hours=1),
            },
        ]

    repository = PendingArrestReviewRepository(SimpleNamespace(fetch_all=fetch_all))

    result = await repository.latest_published_full_reviews(
        guild_id=99,
        report_ids=(10, 10),
        channel_ids=(100,),
    )

    assert len(statements) == 1
    assert result[10].verdict is ReviewVerdict.CORRECTIONS_REQUIRED
    assert result[10].root_message_id == 1002
    statement = str(statements[0])
    assert "report_arrest_bindings" in statement
    assert "report_reviews" in statement
    assert "review_scope" in statement


@pytest.mark.asyncio
async def test_service_rejects_source_sets_above_configured_limit() -> None:
    async def search(_faction_id: int, **_kwargs: object) -> SarpFactionArrestSearchPayload:
        return SarpFactionArrestSearchPayload(data=[], total=1001)

    service = PendingArrestReportService(
        SimpleNamespace(search_faction_arrests=search)
    )
    with pytest.raises(PendingReportScanTooLargeError) as captured:
        await service.scan(
            faction_id=3,
            date_from=date(2026, 8, 1),
            date_to=date(2026, 8, 18),
            max_records=1000,
        )
    assert captured.value.total == 1001
    assert captured.value.limit == 1000


@pytest.mark.asyncio
async def test_command_rejects_members_outside_operational_staff(persona) -> None:
    responses: list[dict[str, object]] = []
    member = _member(11, (999,))

    async def cached(_guild_id: int):
        return SimpleNamespace(
            settings=SimpleNamespace(
                operational_staff_role_ids=(200,),
                pending_arrest_reports_faction_id=3,
            ),
            persona=SimpleNamespace(profile=persona),
        )

    async def respond(**kwargs: object) -> None:
        responses.append(kwargs)

    cog = PendingArrestReportCog(
        SimpleNamespace(cached=cached),
        SimpleNamespace(),
        persona,
    )
    context = SimpleNamespace(guild=member.guild, author=member, respond=respond)

    await PendingArrestReportCog.pending_arrest_reports.callback(  # type: ignore[arg-type]
        cog,
        context,
    )

    assert responses[0]["ephemeral"] is True
    assert responses[0]["embed"].description == (
        persona.messages.interface.pending_arrest_reports.unauthorized
    )


@pytest.mark.asyncio
async def test_command_opens_modal_for_operational_staff_in_any_channel(persona) -> None:
    modals: list[PendingArrestReportModal] = []
    member = _member(11, (200,))

    async def cached(_guild_id: int):
        return SimpleNamespace(
            settings=SimpleNamespace(
                operational_staff_role_ids=(200,),
                pending_arrest_reports_faction_id=3,
            ),
            persona=SimpleNamespace(profile=persona),
        )

    async def send_modal(modal: PendingArrestReportModal) -> None:
        modals.append(modal)

    cog = PendingArrestReportCog(
        SimpleNamespace(cached=cached),
        SimpleNamespace(),
        persona,
    )
    context = SimpleNamespace(
        guild=member.guild,
        author=member,
        send_modal=send_modal,
    )

    await PendingArrestReportCog.pending_arrest_reports.callback(  # type: ignore[arg-type]
        cog,
        context,
    )

    assert len(modals) == 1
    assert modals[0].requester_id == member.id


@pytest.mark.asyncio
async def test_modal_requires_visibility_choice_and_offers_both_scopes() -> None:
    modal = PendingArrestReportModal(SimpleNamespace(), requester_id=42)

    assert modal.title == "Buscar reportes pendientes"
    assert {option.value for option in modal.visibility.options} == {
        PendingReportVisibility.PUBLIC.value,
        PendingReportVisibility.PRIVATE.value,
    }
    assert next(option for option in modal.visibility.options if option.default).value == "public"


@pytest.mark.asyncio
async def test_private_single_page_result_omits_the_absent_pagination_view(persona) -> None:
    member = _member(11, (200,))
    followups: list[dict[str, object]] = []
    deferrals: list[dict[str, object]] = []
    result = PendingArrestReportScan(
        faction_id=3,
        date_from=date(2026, 8, 14),
        date_to=date(2026, 8, 20),
        total_reports=4,
    )

    async def cached(_guild_id: int):
        return SimpleNamespace(
            settings=SimpleNamespace(
                operational_staff_role_ids=(200,),
                pending_arrest_reports_faction_id=3,
                pending_arrest_reports_max_range_days=90,
                pending_arrest_reports_max_records=1000,
                report_channel_allowlist=SimpleNamespace(ids=frozenset({456})),
            ),
            persona=SimpleNamespace(profile=persona),
        )

    async def scan(**_kwargs: object) -> PendingArrestReportScan:
        return result

    async def defer(**kwargs: object) -> None:
        deferrals.append(kwargs)

    async def send(**kwargs: object) -> None:
        followups.append(kwargs)

    cog = PendingArrestReportCog(
        SimpleNamespace(cached=cached),
        SimpleNamespace(scan=scan),
        persona,
    )
    interaction = SimpleNamespace(
        guild=member.guild,
        channel=SimpleNamespace(id=456),
        user=member,
        response=SimpleNamespace(defer=defer),
        followup=SimpleNamespace(send=send),
    )

    await cog.scan(  # type: ignore[arg-type]
        interaction,
        requester_id=member.id,
        period=PendingReportPeriod.LAST_7_DAYS,
        raw_date_from="",
        raw_date_to="",
        visibility=PendingReportVisibility.PRIVATE,
    )

    assert deferrals == [{"ephemeral": True}]
    assert followups[0]["ephemeral"] is True
    assert "view" not in followups[0]
    assert followups[0]["embed"].title == "⏳ Reportes de arresto pendientes"


def test_custom_range_validation_is_bounded() -> None:
    assert _resolve_date_range(
        PendingReportPeriod.CUSTOM,
        "2026-08-01",
        "2026-08-18",
        max_days=90,
    ) == (date(2026, 8, 1), date(2026, 8, 18))
    with pytest.raises(ValueError):
        _resolve_date_range(
            PendingReportPeriod.CUSTOM,
            "2026-01-01",
            "2026-08-18",
            max_days=90,
        )
    with pytest.raises(ValueError):
        _resolve_date_range(
            PendingReportPeriod.CUSTOM,
            "18-08-2026",
            "2026-08-18",
            max_days=90,
        )


def test_embed_is_single_compact_page_with_worst_status_color(persona) -> None:
    created_at = datetime(2026, 8, 17, 10, tzinfo=UTC)
    reports = tuple(
        PendingArrestReport(
            report_id=index,
            officer_name=f"Officer {index}",
            suspect_name=f"Suspect {index}",
            created_at=created_at - timedelta(hours=index),
            status=(
                PendingReportStatus.OVER_MAXIMUM_LIMIT
                if index == 1
                else PendingReportStatus.ON_TIME
            ),
            reason=PendingReportReason.PLACEHOLDER,
        )
        for index in range(1, 17)
    )
    result = PendingArrestReportScan(
        faction_id=3,
        date_from=date(2026, 8, 1),
        date_to=date(2026, 8, 18),
        total_reports=20,
        pending_reports=reports,
    )

    embed = build_pending_arrest_reports_embed(
        result,
        ui=persona.messages.interface.pending_arrest_reports,
    )

    assert embed.color == discord.Color.red()
    assert len(embed.fields) == 0
    assert "📄 **Revisados:** 20" in embed.description
    assert "⏳ **Pendientes:** 16" in embed.description
    assert "✅ **Cumplimiento:** 20,0 %" in embed.description
    assert "👮 **Agentes con pendientes:** 16" in embed.description
    assert "🔴 **#1**" in embed.description
    assert "### 👮 Officer 1 · 1 pendiente" in embed.description
    assert "🔒 Suspect 1" in embed.description
    assert "Agente:" not in embed.description
    assert "Sospechoso:" not in embed.description
    assert embed.footer.text == "Página 1 de 2"
    assert "<t:" in embed.description


def test_embed_without_pending_reports_is_green(persona) -> None:
    result = PendingArrestReportScan(
        faction_id=3,
        date_from=date(2026, 8, 1),
        date_to=date(2026, 8, 18),
        total_reports=4,
    )

    embed = build_pending_arrest_reports_embed(
        result,
        ui=persona.messages.interface.pending_arrest_reports,
    )

    assert embed.color == discord.Color.green()
    assert "⏳ **Pendientes:** 0" in embed.description
    assert "✅ **Cumplimiento:** 100,0 %" in embed.description


def test_embed_lists_unapproved_reviews_in_a_dedicated_compact_section(persona) -> None:
    now = datetime(2026, 8, 18, 12, tzinfo=UTC)
    result = PendingArrestReportScan(
        faction_id=3,
        date_from=date(2026, 8, 1),
        date_to=date(2026, 8, 18),
        total_reports=3,
        unapproved_reports=(
            UnapprovedArrestReport(
                report_id=10,
                officer_name="Officer One",
                suspect_name="Suspect One",
                created_at=now,
            ),
            UnapprovedArrestReport(
                report_id=11,
                officer_name="Officer Two",
                suspect_name="Suspect Two",
                created_at=now,
                review_verdict=ReviewVerdict.CORRECTIONS_REQUIRED,
                review_url="https://discord.com/channels/99/100/1001",
            ),
            UnapprovedArrestReport(
                report_id=12,
                officer_name="Officer Three",
                suspect_name="Suspect Three",
                created_at=now,
                review_verdict=ReviewVerdict.INCONCLUSIVE,
                review_url="https://discord.com/channels/99/100/1002",
            ),
        ),
    )

    embed = build_pending_arrest_reports_embed(
        result,
        ui=persona.messages.interface.pending_arrest_reports,
    )
    description = embed.description or ""

    assert embed.color == discord.Color.gold()
    assert "La redacción está al día" in description
    assert "🔎 **Sin aprobación:** 3" in description
    assert "### 🔎 Revisiones aún no aprobadas" in description
    assert "❌ **#10** · Suspect One · Sin revisión" in description
    assert "🟡 **#11** · Suspect Two · Correcciones pendientes" in description
    assert "⚪ **#12** · Suspect Three · Revisión inconclusa" in description
    assert "[Abrir](https://discord.com/channels/99/100/1001)" in description


def test_combined_pending_and_unapproved_page_stays_within_discord_limit(persona) -> None:
    now = datetime(2026, 8, 18, 12, tzinfo=UTC)
    pending = tuple(
        PendingArrestReport(
            report_id=index,
            officer_name=f"Officer {'X' * 80} {index}",
            suspect_name=f"Suspect {'Y' * 80} {index}",
            created_at=now - timedelta(hours=index),
            status=PendingReportStatus.ON_TIME,
            reason=PendingReportReason.PLACEHOLDER,
        )
        for index in range(1, 9)
    )
    unapproved = tuple(
        UnapprovedArrestReport(
            report_id=100 + index,
            officer_name=f"Officer {index}",
            suspect_name=f"Suspect {'Z' * 80} {index}",
            created_at=now - timedelta(hours=index),
        )
        for index in range(1, 9)
    )
    result = PendingArrestReportScan(
        faction_id=3,
        date_from=date(2026, 8, 1),
        date_to=date(2026, 8, 18),
        total_reports=16,
        pending_reports=pending,
        unapproved_reports=unapproved,
    )

    embed = build_pending_arrest_reports_embed(
        result,
        ui=persona.messages.interface.pending_arrest_reports,
    )

    assert len(embed.description or "") <= 4096


def test_embed_groups_officers_by_pending_count_and_reports_by_age(persona) -> None:
    now = datetime(2026, 8, 18, 12, tzinfo=UTC)
    reports = (
        PendingArrestReport(
            report_id=1,
            officer_name="Bravo Officer",
            suspect_name="Suspect One",
            created_at=now - timedelta(hours=3),
            status=PendingReportStatus.ON_TIME,
            reason=PendingReportReason.PLACEHOLDER,
        ),
        PendingArrestReport(
            report_id=2,
            officer_name="Alpha Officer",
            suspect_name="Suspect Two",
            created_at=now - timedelta(hours=2),
            status=PendingReportStatus.ON_TIME,
            reason=PendingReportReason.PLACEHOLDER,
        ),
        PendingArrestReport(
            report_id=3,
            officer_name="Alpha Officer",
            suspect_name="Suspect Three",
            created_at=now - timedelta(hours=5),
            status=PendingReportStatus.ON_TIME,
            reason=PendingReportReason.PLACEHOLDER,
        ),
    )
    result = PendingArrestReportScan(
        faction_id=3,
        date_from=date(2026, 8, 17),
        date_to=date(2026, 8, 18),
        total_reports=3,
        pending_reports=reports,
    )

    embed = build_pending_arrest_reports_embed(
        result,
        ui=persona.messages.interface.pending_arrest_reports,
    )
    description = embed.description or ""

    assert description.index("Alpha Officer · 2 pendientes") < description.index(
        "Bravo Officer · 1 pendiente"
    )
    assert description.index("**#3**") < description.index("**#2**")


def test_embed_repeats_officer_context_when_group_continues_on_next_page(persona) -> None:
    now = datetime(2026, 8, 18, 12, tzinfo=UTC)
    reports = tuple(
        PendingArrestReport(
            report_id=index,
            officer_name="Michael Falcone",
            suspect_name=f"Suspect {index}",
            created_at=now - timedelta(hours=index),
            status=PendingReportStatus.ON_TIME,
            reason=PendingReportReason.PLACEHOLDER,
        )
        for index in range(1, 17)
    )
    result = PendingArrestReportScan(
        faction_id=3,
        date_from=date(2026, 8, 17),
        date_to=date(2026, 8, 18),
        total_reports=16,
        pending_reports=reports,
    )

    embed = build_pending_arrest_reports_embed(
        result,
        ui=persona.messages.interface.pending_arrest_reports,
        page=1,
    )

    assert "### 👮 Michael Falcone · 16 pendientes · continuación" in embed.description
    assert embed.footer.text == "Página 2 de 2"

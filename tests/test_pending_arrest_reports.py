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
    ArrestReportReviewReference,
    ArrestReviewSource,
    ArrestReviewStatus,
    DiscordLegacyArrestReviewLookup,
    PendingArrestReport,
    PendingArrestReportCog,
    PendingArrestReportModal,
    PendingArrestReportScan,
    PendingArrestReportService,
    PendingArrestReviewRepository,
    PendingReportReason,
    PendingReportScanTooLargeError,
    PendingReportSeverity,
    PendingReportStatus,
    PendingReportVisibility,
    build_pending_arrest_reports_embed,
    pending_report_score,
    severity_for_report,
    status_for_age,
    unfinished_reason,
)
from arrest_report_agent.pending_arrest_reports.discord import (
    PendingReportPaginationView,
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


def _reference(
    report_id: int,
    *,
    approved: bool,
    reviewed_at: datetime,
    channel_id: int = 100,
    message_id: int | None = None,
    source: ArrestReviewSource = ArrestReviewSource.STRUCTURED,
) -> ArrestReportReviewReference:
    return ArrestReportReviewReference(
        report_id=report_id,
        source=source,
        approved=approved,
        guild_id=99,
        parent_channel_id=channel_id,
        root_message_id=message_id or 1000 + report_id,
        reviewed_at=reviewed_at,
    )


def _pending(
    report_id: int,
    *,
    officer: str = "Officer One",
    suspect: str | None = None,
    created_at: datetime,
    pcu_missing: bool,
    review_status: ArrestReviewStatus,
    age_hours: int = 0,
    review_age_hours: int = 0,
) -> PendingArrestReport:
    has_review = review_status is not ArrestReviewStatus.MISSING
    return PendingArrestReport(
        report_id=report_id,
        officer_name=officer,
        suspect_name=suspect or f"Suspect {report_id}",
        created_at=created_at,
        age_hours=age_hours,
        status=PendingReportStatus.ON_TIME,
        pcu_reason=(PendingReportReason.PLACEHOLDER if pcu_missing else None),
        review_status=review_status,
        review_url=(
            None
            if review_status is ArrestReviewStatus.MISSING
            else f"https://discord.com/channels/99/100/{1000 + report_id}"
        ),
        reviewed_at=(created_at if has_review else None),
        review_age_hours=(review_age_hours if has_review else None),
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


def test_severity_and_score_use_independent_pcu_and_review_deadlines() -> None:
    now = datetime(2026, 8, 18, 12, tzinfo=UTC)
    both_missing = _pending(
        1,
        created_at=now - timedelta(hours=8),
        pcu_missing=True,
        review_status=ArrestReviewStatus.MISSING,
        age_hours=8,
    )
    pcu_complete = _pending(
        2,
        created_at=now - timedelta(hours=8),
        pcu_missing=False,
        review_status=ArrestReviewStatus.MISSING,
        age_hours=8,
    )
    pcu_overdue = _pending(
        3,
        created_at=now - timedelta(hours=24),
        pcu_missing=True,
        review_status=ArrestReviewStatus.PENDING,
        age_hours=24,
        review_age_hours=2,
    )
    review_near_deadline = _pending(
        4,
        created_at=now - timedelta(hours=36),
        pcu_missing=False,
        review_status=ArrestReviewStatus.PENDING,
        age_hours=36,
        review_age_hours=4,
    )
    review_overdue = _pending(
        5,
        created_at=now - timedelta(hours=48),
        pcu_missing=False,
        review_status=ArrestReviewStatus.MISSING,
        age_hours=48,
    )
    approved_without_pcu = _pending(
        6,
        created_at=now - timedelta(hours=2),
        pcu_missing=True,
        review_status=ArrestReviewStatus.APPROVED,
        age_hours=2,
    )

    assert severity_for_report(both_missing) is PendingReportSeverity.HIGH
    assert severity_for_report(pcu_complete) is PendingReportSeverity.ON_TIME
    assert severity_for_report(pcu_overdue) is PendingReportSeverity.CRITICAL
    assert severity_for_report(review_near_deadline) is PendingReportSeverity.MEDIUM
    assert severity_for_report(review_overdue) is PendingReportSeverity.CRITICAL
    assert severity_for_report(approved_without_pcu) is PendingReportSeverity.CRITICAL
    assert pending_report_score(both_missing) > pending_report_score(pcu_complete)


@pytest.mark.asyncio
async def test_service_paginates_and_keeps_incomplete_pcu_even_when_review_is_approved() -> None:
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

    async def latest_reviews(**kwargs: object) -> dict[int, ArrestReportReviewReference]:
        return {
            report_id: _reference(report_id, approved=True, reviewed_at=now)
            for report_id in kwargs["report_ids"]
        }

    service = PendingArrestReportService(
        SimpleNamespace(search_faction_arrests=search),
        SimpleNamespace(latest_reviews=latest_reviews),
    )
    result = await service.scan(
        faction_id=3,
        date_from=date(2026, 8, 1),
        date_to=date(2026, 8, 18),
        now=now,
        guild_id=99,
    )

    assert [report.report_id for report in result.pending_reports] == [51, 1]
    assert all(report.review_status is ArrestReviewStatus.APPROVED for report in result.pending_reports)
    assert result.total_reports == 51
    assert len(calls) == 2
    assert calls[0]["date_to"] == date(2026, 8, 19)
    assert calls[0]["limit"] == 50


@pytest.mark.asyncio
async def test_service_unifies_all_pending_reason_combinations() -> None:
    now = datetime(2026, 8, 18, 12, tzinfo=UTC)
    reports = [
        _summary(
            index,
            description=(
                "pendiente"
                if index in {4, 5, 6}
                else "Yo, Officer Example, redacté un informe completo."
            ),
            created_at=now - timedelta(hours=index),
        )
        for index in range(1, 7)
    ]
    lookup_calls: list[dict[str, object]] = []

    async def search(_faction_id: int, **_kwargs: object) -> SarpFactionArrestSearchPayload:
        return SarpFactionArrestSearchPayload(data=reports, total=len(reports))

    async def latest_reviews(**kwargs: object) -> dict[int, ArrestReportReviewReference]:
        lookup_calls.append(kwargs)
        return {
            1: _reference(1, approved=True, reviewed_at=now),
            2: _reference(2, approved=False, reviewed_at=now),
            4: _reference(4, approved=True, reviewed_at=now),
            5: _reference(5, approved=False, reviewed_at=now),
        }

    service = PendingArrestReportService(
        SimpleNamespace(search_faction_arrests=search),
        SimpleNamespace(latest_reviews=latest_reviews),
    )
    result = await service.scan(
        faction_id=3,
        date_from=date(2026, 8, 1),
        date_to=date(2026, 8, 18),
        now=now,
        guild_id=99,
        report_channel_ids=frozenset({100, 200}),
    )

    by_id = {report.report_id: report for report in result.pending_reports}
    assert set(by_id) == {2, 3, 4, 5, 6}
    assert by_id[2].pcu_reason is None
    assert by_id[2].review_status is ArrestReviewStatus.PENDING
    assert by_id[3].review_status is ArrestReviewStatus.MISSING
    assert by_id[4].pcu_reason is PendingReportReason.PLACEHOLDER
    assert by_id[4].review_status is ArrestReviewStatus.APPROVED
    assert by_id[4].reviewed_at == now
    assert by_id[4].review_age_hours == 0
    assert by_id[5].review_status is ArrestReviewStatus.PENDING
    assert by_id[6].review_status is ArrestReviewStatus.MISSING
    assert result.completed_count == 1
    assert lookup_calls == [
        {
            "guild_id": 99,
            "report_ids": (1, 2, 3, 4, 5, 6),
            "channel_ids": frozenset({100, 200}),
        }
    ]


@pytest.mark.asyncio
async def test_service_verifies_legacy_roots_and_treats_missing_posts_as_no_review() -> None:
    now = datetime(2026, 8, 18, 12, tzinfo=UTC)
    reports = [
        _summary(
            report_id,
            description="Yo, Officer Example, redacté un informe completo.",
            created_at=now - timedelta(days=1),
        )
        for report_id in (1, 2, 3)
    ]
    legacy_calls: list[dict[str, object]] = []

    async def search(_faction_id: int, **_kwargs: object) -> SarpFactionArrestSearchPayload:
        return SarpFactionArrestSearchPayload(data=reports, total=3)

    async def structured(**_kwargs: object) -> dict[int, ArrestReportReviewReference]:
        return {
            1: _reference(
                1,
                approved=False,
                reviewed_at=now - timedelta(hours=2),
                source=ArrestReviewSource.LEGACY,
            ),
            2: _reference(2, approved=False, reviewed_at=now, message_id=2002),
            3: _reference(
                3,
                approved=False,
                reviewed_at=now - timedelta(hours=1),
                source=ArrestReviewSource.LEGACY,
            ),
        }

    async def legacy(**kwargs: object) -> dict[int, ArrestReportReviewReference]:
        legacy_calls.append(kwargs)
        return {
            1: _reference(
                1,
                approved=True,
                reviewed_at=now - timedelta(hours=2),
                source=ArrestReviewSource.LEGACY,
            ),
        }

    service = PendingArrestReportService(
        SimpleNamespace(search_faction_arrests=search),
        SimpleNamespace(latest_reviews=structured),
        SimpleNamespace(resolve_approvals=legacy),
    )
    result = await service.scan(
        faction_id=3,
        date_from=date(2026, 8, 1),
        date_to=date(2026, 8, 18),
        now=now,
        guild_id=99,
        report_channel_ids=frozenset({100, 200}),
        authorized_reviewer_role_ids=frozenset({42}),
    )

    assert [report.report_id for report in result.pending_reports] == [2, 3]
    assert result.pending_reports[0].review_url.endswith("/2002")
    assert result.pending_reports[1].review_status is ArrestReviewStatus.MISSING
    assert result.pending_reports[1].review_url is None
    assert legacy_calls[0]["authorized_role_ids"] == frozenset({42})
    assert legacy_calls[0]["after"] == now - timedelta(days=1)
    assert [reference.report_id for reference in legacy_calls[0]["references"]] == [1, 3]


@pytest.mark.asyncio
async def test_review_repository_uses_newest_bound_root_and_its_latest_full_review() -> None:
    now = datetime(2026, 8, 18, 12, tzinfo=UTC)
    statements: list[object] = []

    async def fetch_all(statement: object):
        statements.append(statement)
        return [
            {
                "report_id": 10,
                "verdict": None,
                "guild_id": 99,
                "parent_channel_id": 200,
                "root_message_id": 1002,
                "reviewed_at": now,
                "structured_submission_id": None,
            },
            {
                "report_id": 10,
                "verdict": ReviewVerdict.APPROVED.value,
                "guild_id": 99,
                "parent_channel_id": 100,
                "root_message_id": 1001,
                "reviewed_at": now - timedelta(hours=1),
                "structured_submission_id": "submission-10",
            },
            {
                "report_id": 11,
                "verdict": ReviewVerdict.APPROVED.value,
                "guild_id": 99,
                "parent_channel_id": 100,
                "root_message_id": 1011,
                "reviewed_at": now,
                "structured_submission_id": "submission-11",
            },
        ]

    repository = PendingArrestReviewRepository(SimpleNamespace(fetch_all=fetch_all))
    result = await repository.latest_reviews(
        guild_id=99,
        report_ids=(10, 10, 11),
        channel_ids=(100, 200),
    )

    assert len(statements) == 1
    assert result[10].approved is False
    assert result[10].source is ArrestReviewSource.LEGACY
    assert result[10].root_message_id == 1002
    assert result[10].jump_url == "https://discord.com/channels/99/200/1002"
    assert result[11].approved is True
    assert result[11].source is ArrestReviewSource.STRUCTURED
    assert result[11].reviewed_at == now
    statement = str(statements[0])
    assert "LEFT OUTER JOIN report_reviews" in statement
    assert "arrest_report_submissions" in statement
    assert "review_scope" in statement
    assert "parent_channel_id" in statement


class FakePermissions:
    view_channel = True
    read_message_history = True


class FakeHistoryChannel:
    def __init__(self, channel_id: int, messages: list[object]) -> None:
        self.id = channel_id
        self.messages = messages
        self.history_calls: list[dict[str, object]] = []

    def permissions_for(self, _member: object) -> FakePermissions:
        return FakePermissions()

    async def history(self, **kwargs: object):
        self.history_calls.append(kwargs)
        for message in self.messages:
            yield message


class FakeReaction:
    def __init__(self, emoji: str, *, mine: bool, reactors: tuple[object, ...] = ()) -> None:
        self.emoji = emoji
        self.me = mine
        self.reactors = reactors

    def users(self):
        async def iterate():
            for reactor in self.reactors:
                yield reactor

        return iterate()


def _legacy_message(
    message_id: int,
    channel: FakeHistoryChannel,
    *,
    content: str,
    reaction: FakeReaction | None = None,
    bot: bool = False,
    attachments: bool = True,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=message_id,
        author=SimpleNamespace(id=500 + message_id, bot=bot),
        webhook_id=None,
        reference=None,
        content=content,
        reactions=[] if reaction is None else [reaction],
        attachments=[SimpleNamespace(filename="report.png")] if attachments else [],
        channel=channel,
        created_at=datetime(2026, 8, 18, 10, message_id % 60, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_legacy_lookup_scans_all_configured_channels_and_trusts_only_authorized_checks(
    monkeypatch,
) -> None:
    authorized = SimpleNamespace(id=700, bot=False, roles=[SimpleNamespace(id=42)])
    unauthorized = SimpleNamespace(id=701, bot=False, roles=[SimpleNamespace(id=99)])
    first = FakeHistoryChannel(100, [])
    second = FakeHistoryChannel(200, [])
    first.messages = [
        _legacy_message(
            10,
            first,
            content="<@999> Peter Kinsella - #10",
            reaction=FakeReaction("✅", mine=False, reactors=(authorized,)),
        ),
        _legacy_message(
            11,
            first,
            content="<@999> Robert Miller - #11",
            reaction=FakeReaction("✅", mine=False, reactors=(unauthorized,)),
        ),
        _legacy_message(13, first, content="Reporte estructurado", bot=True),
        _legacy_message(15, first, content="Sin ID visible", attachments=True),
    ]
    second.messages = [
        _legacy_message(
            12,
            second,
            content="<@999> Michael Carter - #12",
            reaction=FakeReaction("✅", mine=True),
        ),
        _legacy_message(
            14,
            second,
            content="<@999> Thomas Walker - #14",
            reaction=FakeReaction("❌", mine=True),
        ),
    ]
    channels = {channel.id: channel for channel in (first, second)}
    guild = SimpleNamespace(
        id=99,
        text_channels=[first, second],
        get_channel=channels.get,
        get_member=lambda member_id: {700: authorized, 701: unauthorized}.get(member_id),
        me=SimpleNamespace(id=999),
    )
    for channel in (first, second):
        for message in channel.messages:
            message.guild = guild
    bot = SimpleNamespace(get_guild=lambda guild_id: guild if guild_id == 99 else None)
    monkeypatch.setattr(
        "arrest_report_agent.pending_arrest_reports.legacy.discord.TextChannel",
        FakeHistoryChannel,
    )

    lookup = DiscordLegacyArrestReviewLookup(bot)
    references = tuple(
        _reference(
            100 + message_id,
            approved=False,
            reviewed_at=datetime(2026, 8, 18, 9, tzinfo=UTC),
            channel_id=(100 if message_id in {10, 11, 13, 15} else 200),
            message_id=message_id,
            source=ArrestReviewSource.LEGACY,
        )
        for message_id in (10, 11, 12, 13, 14, 15)
    )
    result = await lookup.resolve_approvals(
        guild_id=99,
        references=references,
        after=datetime(2026, 8, 1, tzinfo=UTC),
        authorized_role_ids=(42,),
    )

    assert set(result) == {110, 111, 112, 114, 115}
    assert result[110].approved is True
    assert result[111].approved is False
    assert result[112].approved is True
    assert result[112].reviewed_at == second.messages[0].created_at
    assert result[114].approved is False
    assert result[115].approved is False
    assert result[112].parent_channel_id == 200
    assert all(channel.history_calls[0]["limit"] is None for channel in (first, second))


@pytest.mark.asyncio
async def test_service_rejects_source_sets_above_configured_limit() -> None:
    async def search(_faction_id: int, **_kwargs: object) -> SarpFactionArrestSearchPayload:
        return SarpFactionArrestSearchPayload(data=[], total=1001)

    service = PendingArrestReportService(SimpleNamespace(search_faction_arrests=search))
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

    cog = PendingArrestReportCog(SimpleNamespace(cached=cached), SimpleNamespace(), persona)
    context = SimpleNamespace(guild=member.guild, author=member, respond=respond)
    await PendingArrestReportCog.pending_arrest_reports.callback(cog, context)  # type: ignore[arg-type]

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

    cog = PendingArrestReportCog(SimpleNamespace(cached=cached), SimpleNamespace(), persona)
    context = SimpleNamespace(guild=member.guild, author=member, send_modal=send_modal)
    await PendingArrestReportCog.pending_arrest_reports.callback(cog, context)  # type: ignore[arg-type]

    assert len(modals) == 1
    assert modals[0].requester_id == member.id


@pytest.mark.asyncio
async def test_modal_only_collects_period_dates_and_visibility() -> None:
    modal = PendingArrestReportModal(SimpleNamespace(), requester_id=42)

    assert modal.title == "Buscar reportes pendientes"
    assert {option.value for option in modal.visibility.options} == {
        PendingReportVisibility.PUBLIC.value,
        PendingReportVisibility.PRIVATE.value,
    }
    assert next(option for option in modal.visibility.options if option.default).value == "public"
    assert len(modal.children) == 4
    assert not hasattr(modal, "review_inclusion")


@pytest.mark.asyncio
async def test_pagination_allows_interactions_from_any_user(persona) -> None:
    result = PendingArrestReportScan(
        faction_id=3,
        date_from=date(2026, 8, 1),
        date_to=date(2026, 8, 18),
        total_reports=0,
    )
    view = PendingReportPaginationView(
        result=result,
        ui=persona.messages.interface.pending_arrest_reports,
    )

    interaction = SimpleNamespace(user=SimpleNamespace(id=999))

    assert await view.interaction_check(interaction) is True  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_private_single_page_result_passes_review_configuration_to_service(persona) -> None:
    member = _member(11, (200,))
    followups: list[dict[str, object]] = []
    deferrals: list[dict[str, object]] = []
    scan_calls: list[dict[str, object]] = []
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
                supervisor_role_allowlist=SimpleNamespace(ids=frozenset({300, 400})),
                pending_arrest_reports_faction_id=3,
                pending_arrest_reports_max_range_days=90,
                pending_arrest_reports_max_records=1000,
                report_channel_allowlist=SimpleNamespace(ids=frozenset({456, 789})),
            ),
            persona=SimpleNamespace(profile=persona),
        )

    async def scan(**kwargs: object) -> PendingArrestReportScan:
        scan_calls.append(kwargs)
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
    assert scan_calls[0]["report_channel_ids"] == frozenset({456, 789})
    assert scan_calls[0]["authorized_reviewer_role_ids"] == frozenset({300, 400})


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


def test_embed_renders_every_reason_combination_inline(persona) -> None:
    now = datetime(2026, 8, 18, 12, tzinfo=UTC)
    reports = (
        _pending(
            1,
            created_at=now,
            pcu_missing=True,
            review_status=ArrestReviewStatus.APPROVED,
        ),
        _pending(
            2,
            created_at=now,
            pcu_missing=False,
            review_status=ArrestReviewStatus.MISSING,
        ),
        _pending(
            3,
            created_at=now,
            pcu_missing=False,
            review_status=ArrestReviewStatus.PENDING,
        ),
        _pending(
            4,
            created_at=now,
            pcu_missing=True,
            review_status=ArrestReviewStatus.MISSING,
        ),
        _pending(
            5,
            created_at=now,
            pcu_missing=True,
            review_status=ArrestReviewStatus.PENDING,
        ),
    )
    result = PendingArrestReportScan(
        faction_id=3,
        date_from=date(2026, 8, 1),
        date_to=date(2026, 8, 18),
        total_reports=6,
        pending_reports=reports,
    )

    embed = build_pending_arrest_reports_embed(
        result,
        ui=persona.messages.interface.pending_arrest_reports,
    )
    description = embed.description or ""

    assert embed.color == discord.Color.red()
    assert "5 de 6 pendientes" in description
    relative_time = f"<t:{int(now.timestamp())}:R>"
    assert (
        f"🔴 **#1** · Suspect 1 · {relative_time} — Sin PCU · "
        "[Revisión aprobada](" in description
    )
    assert (
        f"🟢 **#2** · Suspect 2 · {relative_time} — Sin revisión"
        in description
    )
    assert (
        f"🟢 **#3** · Suspect 3 · {relative_time} — "
        "[Revisión pendiente]("
    ) in description
    assert (
        f"🟠 **#4** · Suspect 4 · {relative_time} — Sin PCU · Sin revisión"
        in description
    )
    assert (
        f"🟠 **#5** · Suspect 5 · {relative_time} — Sin PCU · "
        "[Revisión pendiente]("
    ) in description
    assert "https://discord.com/channels/99/100/1001" in description
    assert description.count(relative_time) == 8
    assert "🔴 Vencido o inconsistente" in description


def test_embed_without_pending_reports_is_green_and_concise(persona) -> None:
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
    assert "0 de 4 pendientes" in (embed.description or "")
    assert "PCU completo" in (embed.description or "")


def test_embed_marks_an_on_time_missing_review_green_without_completed_pcu_text(
    persona,
) -> None:
    now = datetime(2026, 8, 18, 12, tzinfo=UTC)
    result = PendingArrestReportScan(
        faction_id=3,
        date_from=date(2026, 8, 17),
        date_to=date(2026, 8, 18),
        total_reports=1,
        pending_reports=(
            _pending(
                1,
                created_at=now - timedelta(hours=8),
                pcu_missing=False,
                review_status=ArrestReviewStatus.MISSING,
                age_hours=8,
            ),
        ),
    )

    embed = build_pending_arrest_reports_embed(
        result,
        ui=persona.messages.interface.pending_arrest_reports,
    )
    description = embed.description or ""

    assert embed.color == discord.Color.green()
    assert "🟢 **#1**" in description
    assert "— Sin revisión" in description
    assert "PCU completo" not in description


def test_embed_groups_officers_by_highest_score_then_reports_by_score(persona) -> None:
    now = datetime(2026, 8, 18, 12, tzinfo=UTC)
    reports = (
        _pending(
            1,
            officer="Bravo Officer",
            created_at=now - timedelta(hours=3),
            pcu_missing=False,
            review_status=ArrestReviewStatus.MISSING,
            age_hours=3,
        ),
        _pending(
            2,
            officer="Alpha Officer",
            created_at=now - timedelta(hours=2),
            pcu_missing=False,
            review_status=ArrestReviewStatus.MISSING,
            age_hours=2,
        ),
        _pending(
            3,
            officer="Alpha Officer",
            created_at=now - timedelta(hours=5),
            pcu_missing=False,
            review_status=ArrestReviewStatus.MISSING,
            age_hours=5,
        ),
    )
    result = PendingArrestReportScan(
        faction_id=3,
        date_from=date(2026, 8, 17),
        date_to=date(2026, 8, 18),
        total_reports=3,
        pending_reports=reports,
    )

    description = build_pending_arrest_reports_embed(
        result,
        ui=persona.messages.interface.pending_arrest_reports,
    ).description or ""

    assert description.index("Alpha Officer") < description.index("Bravo Officer")
    assert description.index("**#3**") < description.index("**#2**")


def test_embed_prioritizes_one_critical_report_over_multiple_medium_reports(persona) -> None:
    now = datetime(2026, 8, 18, 12, tzinfo=UTC)
    reports = (
        _pending(
            1,
            officer="Medium Officer",
            created_at=now - timedelta(hours=10),
            pcu_missing=False,
            review_status=ArrestReviewStatus.MISSING,
            age_hours=10,
        ),
        _pending(
            2,
            officer="Medium Officer",
            created_at=now - timedelta(hours=9),
            pcu_missing=False,
            review_status=ArrestReviewStatus.MISSING,
            age_hours=9,
        ),
        _pending(
            3,
            officer="Critical Officer",
            created_at=now - timedelta(hours=25),
            pcu_missing=True,
            review_status=ArrestReviewStatus.MISSING,
            age_hours=25,
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

    assert description.index("Critical Officer") < description.index("Medium Officer")
    assert "🔴 **#3**" in description
    assert "Sin PCU (1 h fuera de plazo)" in description
    assert embed.color == discord.Color.red()


def test_embed_repeats_officer_context_when_group_continues_on_next_page(persona) -> None:
    now = datetime(2026, 8, 18, 12, tzinfo=UTC)
    reports = tuple(
        _pending(
            index,
            officer="Michael Falcone",
            created_at=now - timedelta(hours=index),
            pcu_missing=False,
            review_status=ArrestReviewStatus.MISSING,
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

    assert "### 👮 Michael Falcone · continuación" in (embed.description or "")
    assert embed.footer.text == "Página 2 de 2"
    assert len(embed.description or "") <= 4096

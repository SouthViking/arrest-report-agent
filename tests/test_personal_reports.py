from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest

from arrest_report_agent.application import (
    PersonalReport,
    PersonalReportPeriod,
    PersonalReportQuery,
    PersonalReportSearchResult,
    PersonalReportStatus,
    PersonalReportStatusFilter,
    PersonalReportSummary,
    build_approved_report_ranking,
    build_open_report_ranking,
)
from arrest_report_agent.infrastructure.discord.personal_reports import (
    DiscordPersonalReportFinder,
    PersonalReportPaginationView,
    PersonalReportsModal,
    build_personal_reports_embed,
    build_personal_summary_embed,
    send_personal_report_result,
)
from arrest_report_agent.structured_arrest_reports import (
    ArrestReportSubmission,
    InitialReviewStatus,
    PublicationStatus,
)


class FakePermissions:
    view_channel = True
    read_message_history = True


class FakeHistoryChannel:
    def __init__(self, channel_id: int, name: str, messages: list[object]) -> None:
        self.id = channel_id
        self.name = name
        self.position = channel_id
        self.messages = messages
        self.history_calls: list[dict[str, object]] = []

    def permissions_for(self, _member) -> FakePermissions:
        return FakePermissions()

    async def history(self, **kwargs):
        self.history_calls.append(kwargs)
        for message in self.messages:
            yield message


class FakeReaction:
    def __init__(
        self,
        emoji: str,
        is_mine: bool,
        reactors: tuple[object, ...],
    ) -> None:
        self.emoji = emoji
        self.me = is_mine
        self._reactors = reactors

    def users(self):
        async def iterate():
            for reactor in self._reactors:
                yield reactor

        return iterate()


class FakeGuild:
    def __init__(
        self,
        guild_id: int,
        channels: list[FakeHistoryChannel],
        members: tuple[object, ...] = (),
    ) -> None:
        self.id = guild_id
        self.text_channels = channels
        self._channels = {channel.id: channel for channel in channels}
        self._members = {member.id: member for member in members}
        self.me = SimpleNamespace(id=999)
        for channel in channels:
            channel.guild = self
            for message in channel.messages:
                message.guild = self

    def get_channel(self, channel_id: int):
        return self._channels.get(channel_id)

    def get_member(self, member_id: int):
        return self._members.get(member_id)


def fake_message(
    message_id: int,
    *,
    channel: FakeHistoryChannel,
    author_id: int = 123,
    content: str = "<@999> Terry Parker",
    attachments: bool = True,
    reactions: tuple[tuple[str, bool], ...] = (),
    reaction_users: tuple[object, ...] = (),
    reference=None,
    thread_id: int | None = None,
    bot: bool = False,
    webhook_id: int | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=message_id,
        author=SimpleNamespace(id=author_id, bot=bot),
        webhook_id=webhook_id,
        channel=channel,
        content=content,
        attachments=[SimpleNamespace(filename="report.png")] if attachments else [],
        reactions=[FakeReaction(emoji, is_mine, reaction_users) for emoji, is_mine in reactions],
        reference=reference,
        thread=SimpleNamespace(id=thread_id) if thread_id else None,
        created_at=datetime(2026, 8, 4, 12, message_id % 60, tzinfo=UTC),
        jump_url=f"https://discord.com/channels/1/{channel.id}/{message_id}",
    )


def structured_submission(
    *,
    root_message_id: int,
    owner_user_id: int,
    thread_id: int,
) -> ArrestReportSubmission:
    now = datetime(2026, 8, 4, tzinfo=UTC)
    return ArrestReportSubmission(
        id=UUID(f"00000000-0000-0000-0000-{root_message_id:012d}"),
        guild_id=1234674521416663070,
        interaction_id=root_message_id + 1000,
        owner_user_id=owner_user_id,
        parent_channel_id=1462992794569281617,
        root_message_id=root_message_id,
        thread_id=thread_id,
        sarp_arrest_id=7896,
        suspect_name="Dante Carranza",
        publication_status=PublicationStatus.PUBLISHED,
        initial_review_status=InitialReviewStatus.COMPLETED,
        created_at=now,
        updated_at=now,
    )


def test_periods_and_status_filters_are_bounded_and_explicit() -> None:
    now = datetime(2026, 8, 20, 16, 30, tzinfo=UTC)

    assert PersonalReportPeriod.CURRENT_MONTH.starts_at(now) == datetime(2026, 8, 1, tzinfo=UTC)
    assert PersonalReportPeriod.LAST_7_DAYS.starts_at(now) == datetime(2026, 8, 13, 16, 30, tzinfo=UTC)
    assert PersonalReportStatusFilter.PENDING.includes(PersonalReportStatus.PENDING)
    assert PersonalReportStatusFilter.PENDING.includes(PersonalReportStatus.IN_REVIEW)
    assert not PersonalReportStatusFilter.PENDING.includes(PersonalReportStatus.INCONCLUSIVE)


@pytest.mark.asyncio
async def test_finder_returns_only_root_report_posts_and_uses_trusted_reactions(
    settings,
    monkeypatch,
) -> None:
    channel = FakeHistoryChannel(1462992794569281617, "gnt-arrestos", [])
    channel.messages = [
        fake_message(1, channel=channel),
        fake_message(2, channel=channel, reactions=(("✅", True),), thread_id=2),
        fake_message(3, channel=channel, reactions=(("✅", False),)),
        fake_message(4, channel=channel, reference=SimpleNamespace(message_id=1)),
        fake_message(5, channel=channel, attachments=False, content="hola"),
        fake_message(6, channel=channel, author_id=456),
        fake_message(
            7,
            channel=channel,
            attachments=False,
            content="Reporte escrito directamente",
            reactions=(("❌", True),),
        ),
    ]
    guild = FakeGuild(1234674521416663070, [channel])
    member = SimpleNamespace(id=123)
    monkeypatch.setattr(
        "arrest_report_agent.infrastructure.discord.personal_reports.discord.TextChannel",
        FakeHistoryChannel,
    )
    finder = DiscordPersonalReportFinder(SimpleNamespace(user=SimpleNamespace(id=999)), settings)

    result = await finder.find(
        guild,  # type: ignore[arg-type]
        member,  # type: ignore[arg-type]
        PersonalReportQuery(period=PersonalReportPeriod.LAST_30_DAYS),
        now=datetime(2026, 8, 5, tzinfo=UTC),
    )

    assert [report.message_id for report in result.reports] == [7, 3, 2, 1]
    assert [report.status for report in result.reports] == [
        PersonalReportStatus.CORRECTIONS_REQUIRED,
        PersonalReportStatus.PENDING,
        PersonalReportStatus.APPROVED,
        PersonalReportStatus.PENDING,
    ]
    assert result.reports[2].thread_url == "https://discord.com/channels/1234674521416663070/2"
    assert result.reports[-1].title == "Terry Parker"
    assert channel.history_calls[0]["limit"] == 500
    assert channel.history_calls[0]["after"] == datetime(2026, 7, 6, tzinfo=UTC)

    supervisor_result = await finder.find_all(
        guild,  # type: ignore[arg-type]
        member,  # type: ignore[arg-type]
        PersonalReportQuery(period=PersonalReportPeriod.LAST_30_DAYS),
        now=datetime(2026, 8, 5, tzinfo=UTC),
    )

    assert [report.message_id for report in supervisor_result.reports] == [7, 6, 3, 2, 1]
    assert supervisor_result.reports[1].author_id == 456


@pytest.mark.asyncio
async def test_finder_attributes_valid_structured_cards_to_their_persisted_owner(
    settings,
    monkeypatch,
) -> None:
    owner = SimpleNamespace(id=456, display_name="C. Harrington")
    channel = FakeHistoryChannel(1462992794569281617, "gnt-arrestos", [])
    channel.messages = [
        fake_message(40, channel=channel, author_id=123),
        fake_message(
            41,
            channel=channel,
            author_id=999,
            attachments=False,
            reactions=(("✅", True),),
            bot=True,
            webhook_id=999,
        ),
        fake_message(
            42,
            channel=channel,
            author_id=999,
            attachments=False,
            reactions=(("✅", True),),
            bot=True,
            webhook_id=999,
        ),
        fake_message(
            43,
            channel=channel,
            author_id=999,
            attachments=False,
            reactions=(("✅", True),),
            bot=True,
            webhook_id=999,
        ),
        fake_message(
            44,
            channel=channel,
            author_id=999,
            attachments=False,
            reactions=(("❌", True),),
            bot=True,
            webhook_id=999,
        ),
    ]
    guild = FakeGuild(1234674521416663070, [channel], members=(owner,))

    class Repository:
        def __init__(self) -> None:
            self.calls: list[tuple[int, tuple[int, ...]]] = []

        async def get_published_by_roots(self, guild_id: int, root_ids: tuple[int, ...]):
            self.calls.append((guild_id, root_ids))
            return (
                structured_submission(root_message_id=41, owner_user_id=owner.id, thread_id=141),
                structured_submission(root_message_id=43, owner_user_id=owner.id, thread_id=143),
                structured_submission(root_message_id=44, owner_user_id=owner.id, thread_id=144),
            )

    repository = Repository()
    monkeypatch.setattr(
        "arrest_report_agent.infrastructure.discord.personal_reports.discord.TextChannel",
        FakeHistoryChannel,
    )
    monkeypatch.setattr(
        "arrest_report_agent.infrastructure.discord.personal_reports.is_submission_card",
        lambda message: message.id in {41, 42, 44},
    )
    finder = DiscordPersonalReportFinder(
        SimpleNamespace(user=SimpleNamespace(id=999)),
        settings,
        repository,  # type: ignore[arg-type]
    )

    result = await finder.find_all(
        guild,  # type: ignore[arg-type]
        owner,  # type: ignore[arg-type]
        PersonalReportQuery(period=PersonalReportPeriod.CURRENT_MONTH),
        now=datetime(2026, 8, 5, tzinfo=UTC),
    )

    assert [report.message_id for report in result.reports] == [44, 41, 40]
    structured = result.reports[1]
    assert structured.author_id == owner.id
    assert structured.author_display_name == "C. Harrington"
    assert structured.status is PersonalReportStatus.APPROVED
    assert structured.title == "Dante Carranza · #7896"
    assert structured.thread_url == "https://discord.com/channels/1234674521416663070/141"
    assert [entry.author_id for entry in build_approved_report_ranking(result.reports)] == [owner.id]
    assert [entry.author_id for entry in build_open_report_ranking(result.reports)] == [123, owner.id]
    assert repository.calls == [
        (1234674521416663070, (41, 42, 43, 44)),
    ]


@pytest.mark.asyncio
async def test_finder_accepts_manual_status_only_from_authorized_supervisors(
    settings,
    monkeypatch,
) -> None:
    authorized = SimpleNamespace(
        id=700,
        bot=False,
        roles=[SimpleNamespace(id=42)],
    )
    unauthorized = SimpleNamespace(
        id=701,
        bot=False,
        roles=[SimpleNamespace(id=99)],
    )
    channel = FakeHistoryChannel(1462992794569281617, "gnt-arrestos", [])
    channel.messages = [
        fake_message(
            20,
            channel=channel,
            reactions=(("✅", False),),
            reaction_users=(authorized,),
        ),
        fake_message(
            21,
            channel=channel,
            reactions=(("✅", False),),
            reaction_users=(unauthorized,),
        ),
        fake_message(22, channel=channel, reactions=(("❌", True),)),
    ]
    guild = FakeGuild(
        1234674521416663070,
        [channel],
        members=(authorized, unauthorized),
    )
    member = SimpleNamespace(id=123)
    monkeypatch.setattr(
        "arrest_report_agent.infrastructure.discord.personal_reports.discord.TextChannel",
        FakeHistoryChannel,
    )
    restricted_settings = settings.model_copy(
        update={"supervisor_role_ids_csv": "42"},
    )
    finder = DiscordPersonalReportFinder(SimpleNamespace(), restricted_settings)

    result = await finder.find(
        guild,  # type: ignore[arg-type]
        member,  # type: ignore[arg-type]
        PersonalReportQuery(period=PersonalReportPeriod.CURRENT_MONTH),
        now=datetime(2026, 8, 5, tzinfo=UTC),
    )

    assert [report.status for report in result.reports] == [
        PersonalReportStatus.CORRECTIONS_REQUIRED,
        PersonalReportStatus.PENDING,
        PersonalReportStatus.APPROVED,
    ]


@pytest.mark.asyncio
async def test_finder_fetches_uncached_supervisor_once_per_query(
    settings,
    monkeypatch,
) -> None:
    authorized = SimpleNamespace(
        id=700,
        bot=False,
        roles=[SimpleNamespace(id=42)],
    )

    class FetchingGuild(FakeGuild):
        def __init__(self, guild_id, channels) -> None:
            super().__init__(guild_id, channels)
            self.fetch_calls: list[int] = []

        def get_member(self, _member_id: int):
            return None

        async def fetch_member(self, member_id: int):
            self.fetch_calls.append(member_id)
            return authorized

    channel = FakeHistoryChannel(1462992794569281617, "gnt-arrestos", [])
    channel.messages = [
        fake_message(
            30,
            channel=channel,
            reactions=(("✅", False),),
            reaction_users=(authorized,),
        ),
        fake_message(
            31,
            channel=channel,
            reactions=(("✅", False),),
            reaction_users=(authorized,),
        ),
    ]
    guild = FetchingGuild(1234674521416663070, [channel])
    member = SimpleNamespace(id=123)
    monkeypatch.setattr(
        "arrest_report_agent.infrastructure.discord.personal_reports.discord.TextChannel",
        FakeHistoryChannel,
    )
    restricted_settings = settings.model_copy(
        update={"supervisor_role_ids_csv": "42"},
    )
    finder = DiscordPersonalReportFinder(SimpleNamespace(), restricted_settings)

    result = await finder.find(
        guild,  # type: ignore[arg-type]
        member,  # type: ignore[arg-type]
        PersonalReportQuery(period=PersonalReportPeriod.CURRENT_MONTH),
        now=datetime(2026, 8, 5, tzinfo=UTC),
    )

    assert all(report.status is PersonalReportStatus.APPROVED for report in result.reports)
    assert guild.fetch_calls == [authorized.id]


@pytest.mark.asyncio
async def test_finder_applies_status_and_channel_filters(settings, monkeypatch) -> None:
    first = FakeHistoryChannel(1462992794569281617, "gnt-arrestos", [])
    second = FakeHistoryChannel(1462992859761217745, "dlm-arrestos", [])
    first.messages = [fake_message(10, channel=first, reactions=(("✅", True),))]
    second.messages = [fake_message(11, channel=second, reactions=(("❌", True),))]
    guild = FakeGuild(1234674521416663070, [first, second])
    member = SimpleNamespace(id=123)
    monkeypatch.setattr(
        "arrest_report_agent.infrastructure.discord.personal_reports.discord.TextChannel",
        FakeHistoryChannel,
    )
    finder = DiscordPersonalReportFinder(SimpleNamespace(), settings)

    result = await finder.find(
        guild,  # type: ignore[arg-type]
        member,  # type: ignore[arg-type]
        PersonalReportQuery(
            period=PersonalReportPeriod.CURRENT_MONTH,
            status=PersonalReportStatusFilter.CORRECTIONS_REQUIRED,
            channel_id=second.id,
        ),
        now=datetime(2026, 8, 5, tzinfo=UTC),
    )

    assert [report.message_id for report in result.reports] == [11]
    assert not first.history_calls


def sample_reports(count: int = 1) -> tuple[PersonalReport, ...]:
    statuses = (
        PersonalReportStatus.APPROVED,
        PersonalReportStatus.CORRECTIONS_REQUIRED,
        PersonalReportStatus.PENDING,
        PersonalReportStatus.IN_REVIEW,
        PersonalReportStatus.INCONCLUSIVE,
    )
    return tuple(
        PersonalReport(
            message_id=100 + index,
            channel_id=200,
            created_at=datetime(2026, 8, index + 1, tzinfo=UTC),
            title=f"Reporte {index + 1}",
            status=statuses[index % len(statuses)],
            post_url=f"https://discord.com/channels/1/200/{100 + index}",
            thread_url=(f"https://discord.com/channels/1/{100 + index}" if index % 2 == 0 else None),
        )
        for index in range(count)
    )


def test_personal_report_embed_exposes_direct_post_and_thread_references(persona) -> None:
    result = PersonalReportSearchResult(reports=sample_reports())
    embed = build_personal_reports_embed(
        result,
        PersonalReportQuery(),
        display_name="Sgt. Rivera",
        ui=persona.messages.interface.personal_reports,
        navigation=persona.messages.interface.navigation,
    )

    assert embed.title == "📁 Mis reportes"
    assert "[Abrir publicación](https://discord.com/channels/1/200/100)" in embed.fields[0].value
    assert "[Abrir hilo](https://discord.com/channels/1/100)" in embed.fields[0].value
    assert "01/08/26" in embed.fields[0].value
    assert "Sgt. Rivera" in embed.footer.text


def test_personal_summary_uses_current_reaction_snapshot(persona) -> None:
    reports = sample_reports(5)
    summary = PersonalReportSummary.from_reports(reports)
    embed = build_personal_summary_embed(
        PersonalReportSearchResult(reports=reports),
        display_name="Sgt. Rivera",
        now=datetime(2026, 8, 20, tzinfo=UTC),
        ui=persona.messages.interface.personal_reports,
    )

    assert summary.total == 5
    assert summary.pending == 2
    assert summary.approval_rate == 20
    assert embed.title == "⭐ Mi resumen · Agosto 2026"
    assert "**Presentados:** 5" in embed.description
    assert "**Aprobación actual:** 20.0%" in embed.description
    assert "reacciones de Sgt. Rivera" in embed.footer.text


@pytest.mark.asyncio
async def test_personal_report_pagination_and_modal_expose_compact_filters(
    settings,
    persona,
) -> None:
    result = PersonalReportSearchResult(reports=sample_reports(6))
    view = PersonalReportPaginationView(
        requester_id=123,
        result=result,
        query=PersonalReportQuery(),
        display_name="Sgt. Rivera",
        messages=persona.messages.access,
        ui=persona.messages.interface.personal_reports,
        navigation=persona.messages.interface.navigation,
    )
    channels = [
        FakeHistoryChannel(1462992794569281617, "gnt-arrestos", []),
        FakeHistoryChannel(1462992859761217745, "dlm-arrestos", []),
    ]
    modal = PersonalReportsModal(
        finder=SimpleNamespace(),  # type: ignore[arg-type]
        guild=SimpleNamespace(id=1),  # type: ignore[arg-type]
        member=SimpleNamespace(id=123),  # type: ignore[arg-type]
        channels=channels,  # type: ignore[arg-type]
        display_name="Sgt. Rivera",
        access_messages=persona.messages.access,
        failure_messages=persona.messages.failures,
        ui=persona.messages.interface.personal_reports,
        navigation=persona.messages.interface.navigation,
    )

    assert view.page_count == 2
    assert view.previous_page.disabled is True
    assert view.next_page.disabled is False
    assert [child.label for child in modal.children] == ["Periodo", "Estado actual", "Estación"]
    assert [option.label for option in modal.channel_select.options] == [
        "Todas las estaciones",
        "#gnt-arrestos",
        "#dlm-arrestos",
    ]


@pytest.mark.asyncio
async def test_single_page_ephemeral_result_omits_null_view(persona) -> None:
    sent: list[dict[str, object]] = []

    async def send(**kwargs) -> None:
        sent.append(kwargs)

    interaction = SimpleNamespace(followup=SimpleNamespace(send=send))
    view = PersonalReportPaginationView(
        requester_id=123,
        result=PersonalReportSearchResult(reports=sample_reports()),
        query=PersonalReportQuery(),
        display_name="Sgt. Rivera",
        messages=persona.messages.access,
        ui=persona.messages.interface.personal_reports,
        navigation=persona.messages.interface.navigation,
    )

    await send_personal_report_result(interaction, view)  # type: ignore[arg-type]

    assert sent[0]["ephemeral"] is True
    assert "view" not in sent[0]


@pytest.mark.asyncio
async def test_multi_page_ephemeral_result_includes_pagination_view(persona) -> None:
    sent: list[dict[str, object]] = []

    async def send(**kwargs) -> None:
        sent.append(kwargs)

    interaction = SimpleNamespace(followup=SimpleNamespace(send=send))
    view = PersonalReportPaginationView(
        requester_id=123,
        result=PersonalReportSearchResult(reports=sample_reports(6)),
        query=PersonalReportQuery(),
        display_name="Sgt. Rivera",
        messages=persona.messages.access,
        ui=persona.messages.interface.personal_reports,
        navigation=persona.messages.interface.navigation,
    )

    await send_personal_report_result(interaction, view)  # type: ignore[arg-type]

    assert sent[0]["view"] is view

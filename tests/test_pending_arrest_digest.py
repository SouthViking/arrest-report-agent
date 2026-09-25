from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from arrest_report_agent.application.ports import GuildMemberProfile
from arrest_report_agent.config import PendingArrestReportsOptions
from arrest_report_agent.pending_arrest_reports import (
    ArrestReviewStatus,
    PendingArrestReport,
    PendingArrestReportScan,
    PendingReportReason,
    PendingReportStatus,
)
from arrest_report_agent.pending_arrest_reports.digest import (
    PendingArrestDigestWorker,
    due_mentions,
    grouped_reply_chunks,
    reminder_stage,
    unique_member_match,
)

NOW = datetime(2026, 9, 25, 12, tzinfo=UTC)


def _report(
    report_id: int,
    *,
    officer: str = "Malcolm_Crawford",
    age_hours: int,
    pcu_missing: bool = False,
    review_status: ArrestReviewStatus = ArrestReviewStatus.MISSING,
) -> PendingArrestReport:
    has_review = review_status is not ArrestReviewStatus.MISSING
    return PendingArrestReport(
        report_id=report_id,
        officer_name=officer,
        suspect_name="Example Suspect",
        created_at=NOW - timedelta(hours=age_hours),
        age_hours=age_hours,
        status=PendingReportStatus.ON_TIME,
        pcu_reason=PendingReportReason.PLACEHOLDER if pcu_missing else None,
        review_status=review_status,
        review_url="https://discord.com/channels/1/2/3" if has_review else None,
        reviewed_at=NOW if has_review else None,
        review_age_hours=0 if has_review else None,
    )


def _member(user_id: int, display_name: str) -> GuildMemberProfile:
    return GuildMemberProfile(
        user_id=user_id,
        display_name=display_name,
        username=f"user{user_id}",
        roles=("Field Staff",),
        joined_at=None,
        created_at=NOW - timedelta(days=30),
        is_bot=False,
        role_ids=(100,),
    )


def test_reminder_stage_uses_pcu_and_review_deadlines_independently() -> None:
    assert reminder_stage(_report(1, age_hours=17, pcu_missing=True)) == 0
    assert reminder_stage(_report(2, age_hours=18, pcu_missing=True)) == 1
    assert reminder_stage(_report(3, age_hours=24, pcu_missing=True)) == 2
    assert reminder_stage(_report(4, age_hours=35)) == 0
    assert reminder_stage(_report(5, age_hours=36)) == 1
    assert reminder_stage(_report(6, age_hours=48)) == 2


def test_name_match_handles_multiple_characters_accents_and_ambiguity() -> None:
    members = (
        _member(1, "C. Harrington / M. Cráwford"),
        _member(2, "R. Castellanos"),
    )
    assert unique_member_match("Malcolm_Crawford", members) == (members[0], False)
    assert unique_member_match("Charles Harrington", members) == (members[0], False)
    assert unique_member_match("Malcolm_Crawford", members + (_member(3, "M. Crawford"),)) == (
        None,
        True,
    )
    assert unique_member_match("Different Person", members) == (None, False)


def test_mentions_only_repeat_after_escalation_or_one_day() -> None:
    reports = (_report(1, age_hours=18, pcu_missing=True),)
    matches = {1: 11}
    assert due_mentions(reports, matches, {}, now=NOW) == ((1, 11, 1),)
    history = {(1, 11): (1, NOW - timedelta(hours=6))}
    assert due_mentions(reports, matches, history, now=NOW) == ()
    assert due_mentions((_report(1, age_hours=24, pcu_missing=True),), matches, history, now=NOW) == (
        (1, 11, 2),
    )
    history = {(1, 11): (2, NOW - timedelta(days=1))}
    assert due_mentions((_report(1, age_hours=48),), matches, history, now=NOW) == ((1, 11, 2),)


def test_grouped_reply_uses_one_mention_per_person_and_no_extra_header() -> None:
    guild = SimpleNamespace(emojis=[])
    chunks = grouped_reply_chunks(guild, ((1, 11, 1), (2, 11, 2), (3, 22, 1)))
    assert len(chunks) == 1
    assert "💀 **Fuera de plazo:** <@11>" in chunks[0]
    assert "🚧 **Próximos a vencer:** <@22>" in chunks[0]
    assert chunks[0].count("<@11>") == 1
    assert "Hay pendientes" not in chunks[0]


class _FakeRepository:
    def __init__(self) -> None:
        self.history: dict[tuple[int, int], tuple[int, datetime]] = {}
        self.delivered: dict[uuid.UUID, dict[int, tuple[int, bool]]] = {}
        self.finished = 0

    async def claim(self, guild_id: int, *, now: datetime, interval_hours: int) -> uuid.UUID:
        return uuid.uuid4()

    async def deliveries(self, guild_id: int, cycle_id: uuid.UUID) -> dict[int, tuple[int, bool]]:
        return self.delivered.setdefault(cycle_id, {})

    async def mention_history(
        self, guild_id: int, report_ids: tuple[int, ...]
    ) -> dict[tuple[int, int], tuple[int, datetime]]:
        return {key: value for key, value in self.history.items() if key[0] in report_ids}

    async def record_delivery(
        self, guild_id: int, cycle_id: uuid.UUID, channel_id: int, message_id: int
    ) -> None:
        self.delivered[cycle_id][channel_id] = (message_id, False)

    async def record_reply(self, guild_id: int, cycle_id: uuid.UUID, channel_id: int) -> None:
        message_id, _ = self.delivered[cycle_id][channel_id]
        self.delivered[cycle_id][channel_id] = (message_id, True)

    async def record_mentions(
        self, guild_id: int, mentions: tuple[tuple[int, int, int], ...], *, now: datetime
    ) -> None:
        for report_id, user_id, stage in mentions:
            self.history[(report_id, user_id)] = (stage, now)

    async def finish(self, guild_id: int, cycle_id: uuid.UUID, *, now: datetime, interval_hours: int) -> None:
        self.finished += 1

    async def retry(self, guild_id: int, cycle_id: uuid.UUID, *, now: datetime) -> None:
        raise AssertionError("No retry expected")


@pytest.mark.asyncio
async def test_worker_scans_once_for_multiple_channels_and_replies_only_when_due(persona) -> None:
    options = PendingArrestReportsOptions(
        faction_id=3,
        digest_targets=(
            {"channel_id": 55, "mention_users": True},
            {"channel_id": 66},
        ),
    )
    settings = SimpleNamespace(
        pending_arrest_reports_options=options,
        report_channel_allowlist=SimpleNamespace(ids=(7,)),
        supervisor_role_allowlist=SimpleNamespace(ids=(8,)),
        operational_staff_role_ids=(100,),
    )
    bundle = SimpleNamespace(
        snapshot=SimpleNamespace(guild_id=1),
        settings=settings,
        persona=SimpleNamespace(profile=persona),
    )
    result = PendingArrestReportScan(
        faction_id=3,
        date_from=date(2026, 9, 19),
        date_to=NOW.date(),
        total_reports=2,
        pending_reports=(
            _report(1, age_hours=49, officer="Malcolm_Crawford"),
            _report(2, age_hours=18, pcu_missing=True, officer="Malcolm_Crawford"),
        ),
    )
    messages: dict[int, list[SimpleNamespace]] = {55: [], 66: []}

    async def send_to(channel_id: int, **kwargs: object) -> SimpleNamespace:
        message = SimpleNamespace(id=1000 + len(messages[channel_id]), replies=[], embed=kwargs["embed"])

        async def reply(content: str, **reply_kwargs: object) -> None:
            message.replies.append((content, reply_kwargs))

        message.reply = reply
        messages[channel_id].append(message)
        return message

    def channel_for(channel_id: int) -> SimpleNamespace:
        async def send(**kwargs: object) -> SimpleNamespace:
            return await send_to(channel_id, **kwargs)

        return SimpleNamespace(send=AsyncMock(side_effect=send))

    channels = {channel_id: channel_for(channel_id) for channel_id in (55, 66)}
    repository = _FakeRepository()
    scan = AsyncMock(return_value=result)
    guild = SimpleNamespace(id=1, emojis=[])
    worker = PendingArrestDigestWorker(
        SimpleNamespace(is_ready=lambda: True, get_guild=lambda _id: guild),
        SimpleNamespace(cached=AsyncMock(return_value=bundle)),
        repository,
        SimpleNamespace(scan=scan),
        SimpleNamespace(),
        enabled_guild_ids=(1,),
    )
    worker._channel = AsyncMock(side_effect=lambda _guild, channel_id: channels[channel_id])
    worker._members = AsyncMock(return_value=(_member(11, "C. Harrington / M. Cráwford"),))

    await worker.process_once(now=NOW)
    assert scan.await_count == 1
    assert len(messages[55]) == len(messages[66]) == 1
    assert len(messages[55][0].replies) == 1
    assert messages[66][0].replies == []
    reply_text, reply_options = messages[55][0].replies[0]
    assert "<@11>" in reply_text and reply_text.count("<@11>") == 1
    assert reply_options["mention_author"] is False
    assert repository.history[(1, 11)][0] == 2
    assert repository.history[(2, 11)][0] == 1

    await worker.process_once(now=NOW + timedelta(hours=6))
    assert scan.await_count == 2
    assert len(messages[55]) == len(messages[66]) == 2
    assert messages[55][1].replies == []
    assert repository.finished == 2


@pytest.mark.asyncio
async def test_worker_posts_nothing_when_all_reports_are_complete(persona) -> None:
    options = PendingArrestReportsOptions(
        faction_id=3, digest_targets=({"channel_id": 55, "mention_users": True},)
    )
    bundle = SimpleNamespace(
        snapshot=SimpleNamespace(guild_id=1),
        settings=SimpleNamespace(
            pending_arrest_reports_options=options,
            report_channel_allowlist=SimpleNamespace(ids=()),
            supervisor_role_allowlist=SimpleNamespace(ids=()),
            operational_staff_role_ids=(100,),
        ),
        persona=SimpleNamespace(profile=persona),
    )
    scan = AsyncMock(
        return_value=PendingArrestReportScan(
            faction_id=3, date_from=NOW.date(), date_to=NOW.date(), total_reports=0
        )
    )
    repository = _FakeRepository()
    worker = PendingArrestDigestWorker(
        SimpleNamespace(is_ready=lambda: True),
        SimpleNamespace(cached=AsyncMock(return_value=bundle)),
        repository,
        SimpleNamespace(scan=scan),
        SimpleNamespace(),
        enabled_guild_ids=(1,),
    )
    worker._channel = AsyncMock()
    await worker.process_once(now=NOW)
    worker._channel.assert_not_awaited()
    assert repository.finished == 1


@pytest.mark.asyncio
async def test_partial_delivery_retry_does_not_duplicate_successful_channel(persona) -> None:
    options = PendingArrestReportsOptions(
        faction_id=3,
        digest_targets=({"channel_id": 55}, {"channel_id": 66}),
    )
    bundle = SimpleNamespace(
        snapshot=SimpleNamespace(guild_id=1),
        persona=SimpleNamespace(profile=persona),
    )
    result = PendingArrestReportScan(
        faction_id=3,
        date_from=NOW.date(),
        date_to=NOW.date(),
        total_reports=1,
        pending_reports=(_report(1, age_hours=36),),
    )
    messages: dict[int, list[int]] = {55: [], 66: []}
    fail_second = True

    async def send_first(**_kwargs: object) -> SimpleNamespace:
        messages[55].append(1001)
        return SimpleNamespace(id=1001)

    async def send_second(**_kwargs: object) -> SimpleNamespace:
        nonlocal fail_second
        if fail_second:
            fail_second = False
            raise RuntimeError("Discord unavailable")
        messages[66].append(2001)
        return SimpleNamespace(id=2001)

    channels = {
        55: SimpleNamespace(send=AsyncMock(side_effect=send_first)),
        66: SimpleNamespace(send=AsyncMock(side_effect=send_second)),
    }
    repository = _FakeRepository()
    worker = PendingArrestDigestWorker(
        SimpleNamespace(get_guild=lambda _id: SimpleNamespace(id=1, emojis=[])),
        SimpleNamespace(),
        repository,
        SimpleNamespace(),
        SimpleNamespace(),
    )
    worker._channel = AsyncMock(side_effect=lambda _guild, channel_id: channels[channel_id])
    cycle_id = uuid.uuid4()

    with pytest.raises(RuntimeError, match="Discord unavailable"):
        await worker._publish(bundle, options, result, cycle_id=cycle_id, now=NOW)
    await worker._publish(bundle, options, result, cycle_id=cycle_id, now=NOW)

    assert messages == {55: [1001], 66: [2001]}


@pytest.mark.asyncio
async def test_interval_configuration_change_reschedules_without_restart() -> None:
    original = PendingArrestReportsOptions(faction_id=3, digest_targets=({"channel_id": 55},))
    updated = PendingArrestReportsOptions(
        faction_id=3, digest_interval_hours=3, digest_targets=({"channel_id": 55},)
    )
    repository = SimpleNamespace(reschedule=AsyncMock())
    worker = PendingArrestDigestWorker(
        SimpleNamespace(),
        SimpleNamespace(),
        repository,
        SimpleNamespace(),
        SimpleNamespace(),
        enabled_guild_ids=(1,),
        initial_options={1: original},
    )
    worker.start = lambda: None
    bundle = SimpleNamespace(
        snapshot=SimpleNamespace(guild_id=1),
        settings=SimpleNamespace(pending_arrest_reports_options=updated),
    )

    await worker.configuration_updated(bundle)

    repository.reschedule.assert_awaited_once()
    assert repository.reschedule.await_args.kwargs["interval_hours"] == 3

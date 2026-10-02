from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import discord
import pytest

from arrest_report_agent.domain import AgentAccessLevel, InteractionMode, ReviewCase
from arrest_report_agent.domain.errors import ReportOwnerSubmissionError
from arrest_report_agent.infrastructure.discord.cog import ReviewCog, build_owner_quota_exhausted_staff_embed
from arrest_report_agent.infrastructure.discord.context import (
    DiscordResponseRoute,
    ReviewTarget,
)
from arrest_report_agent.report_owner_access import (
    OwnerRoundGrant,
    OwnerRunReservation,
    OwnerRunReservationResult,
)


class _Typing:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *_args: object) -> None:
        return None


class _Channel:
    def __init__(self, channel_id: int) -> None:
        self.id = channel_id

    def typing(self) -> _Typing:
        return _Typing()


class _Presence:
    @asynccontextmanager
    async def handling(self):
        yield _Activity()


class _Activity:
    async def transition(self, *_args: object) -> None:
        return None


class _Publisher:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        pass


class _OwnerContextBuilder:
    def __init__(self, target: ReviewTarget, case: ReviewCase) -> None:
        self.target = target
        self.case = case
        self.owner_resolutions = 0
        self.case_builds = 0

    async def resolve_report_owner_target(
        self,
        *_args: object,
        **_kwargs: object,
    ) -> ReviewTarget:
        self.owner_resolutions += 1
        return self.target

    async def build_case(self, target: ReviewTarget, *, include_channel_history: bool) -> ReviewCase:
        assert target is self.target
        assert include_channel_history is False
        self.case_builds += 1
        return self.case


class _FailingOwnerContextBuilder(_OwnerContextBuilder):
    async def build_case(self, target: ReviewTarget, *, include_channel_history: bool) -> ReviewCase:
        raise RuntimeError("context unavailable before OpenAI")


class _InvalidOwnerContextBuilder(_OwnerContextBuilder):
    async def resolve_report_owner_target(self, *_args: object, **_kwargs: object) -> ReviewTarget:
        raise ReportOwnerSubmissionError("invalid owner report")


class _Service:
    def __init__(self) -> None:
        self.model = SimpleNamespace(
            id="gpt-test",
            reasoning_effort="low",
            runtime_defaults=SimpleNamespace(image_detail="auto"),
        )
        self.contexts: list[object] = []

    async def review(self, context, _agent_input: object) -> None:
        self.contexts.append(context)
        context.messages_sent_count = 1
        context.completed = True


class _Repository:
    def __init__(self, reservation: OwnerRunReservation) -> None:
        self.reservation = reservation
        self.reservations = 0
        self.completed: list[object] = []
        self.failed: list[object] = []

    async def reserve(self, **_kwargs: object) -> OwnerRunReservation:
        self.reservations += 1
        return self.reservation

    async def mark_completed(self, run_id: object) -> None:
        self.completed.append(run_id)

    async def mark_failed(self, run_id: object) -> None:
        self.failed.append(run_id)


@pytest.mark.asyncio
async def test_grant_report_owner_rounds_rejects_other_members(settings, persona) -> None:
    repository = SimpleNamespace(grant_extra_rounds=AsyncMock())
    cog = ReviewCog(
        SimpleNamespace(user=SimpleNamespace(id=999)),  # type: ignore[arg-type]
        settings,
        _Service(),  # type: ignore[arg-type]
        persona,
        report_owner_run_repository=repository,  # type: ignore[arg-type]
    )
    cog._default_runtime = replace(  # noqa: SLF001
        cog._default_runtime,  # noqa: SLF001
        settings=SimpleNamespace(
            report_owner_quota_admin_user_ids=(123, 125),
            report_owner_quota_grant_role_ids=(200, 201),
            report_owner_access_max_rounds=5,
        ),
    )
    context = SimpleNamespace(
        author=SimpleNamespace(id=124, roles=[SimpleNamespace(id=202)]),
        respond=AsyncMock(),
    )

    await ReviewCog.grant_report_owner_rounds.callback(cog, context, 2)  # type: ignore[arg-type]

    context.respond.assert_awaited_once()
    assert context.respond.await_args.kwargs["ephemeral"] is True
    repository.grant_extra_rounds.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("admin_id", "role_id", "used_rounds", "available"),
    [(123, None, 5, 2), (125, None, 2, 5), (124, 200, 3, 4), (124, 201, 1, 6)],
)
async def test_grant_report_owner_rounds_publishes_current_availability(
    settings, persona, admin_id: int, role_id: int | None, used_rounds: int, available: int
) -> None:
    repository = SimpleNamespace(
        grant_extra_rounds=AsyncMock(
            return_value=OwnerRoundGrant(added_rounds=2, used_rounds=used_rounds, max_rounds=7)
        )
    )
    cog = ReviewCog(
        SimpleNamespace(user=SimpleNamespace(id=999)),  # type: ignore[arg-type]
        settings,
        _Service(),  # type: ignore[arg-type]
        persona,
        report_owner_run_repository=repository,  # type: ignore[arg-type]
    )
    builder = SimpleNamespace(report_owner_for_thread=AsyncMock(return_value=(700, 456)))
    cog._default_runtime = replace(  # noqa: SLF001
        cog._default_runtime,  # noqa: SLF001
        settings=SimpleNamespace(
            report_owner_quota_admin_user_ids=(123, 125),
            report_owner_quota_grant_role_ids=(200, 201),
            report_owner_access_max_rounds=5,
        ),
        context_builder=builder,  # type: ignore[arg-type]
    )
    thread = MagicMock(spec=discord.Thread)
    thread.id = 700
    thread.send = AsyncMock(return_value=SimpleNamespace(id=900))
    context = SimpleNamespace(
        author=SimpleNamespace(
            id=admin_id,
            roles=[SimpleNamespace(id=role_id)] if role_id is not None else [],
        ),
        guild=SimpleNamespace(id=100),
        channel=thread,
        interaction=SimpleNamespace(id=800),
        defer=AsyncMock(),
        send_followup=AsyncMock(),
    )

    await ReviewCog.grant_report_owner_rounds.callback(cog, context, 2)  # type: ignore[arg-type]

    context.defer.assert_awaited_once_with(ephemeral=True)
    builder.report_owner_for_thread.assert_awaited_once_with(thread)
    repository.grant_extra_rounds.assert_awaited_once_with(
        guild_id=100,
        root_message_id=700,
        owner_user_id=456,
        granted_by_user_id=admin_id,
        interaction_id=800,
        amount=2,
        base_max_rounds=5,
    )
    thread.send.assert_awaited_once()
    public_args = thread.send.await_args.kwargs
    assert public_args["embed"].title == "🎟️ Cupo de revisión ampliado"
    assert f"**Turnos disponibles ahora:** {available}" in public_args["embed"].description
    assert "**Autorizado por:**" in public_args["embed"].description
    assert public_args["allowed_mentions"].everyone is False
    assert context.send_followup.await_args.kwargs["ephemeral"] is True


@pytest.mark.asyncio
async def test_grant_report_owner_rounds_does_not_publish_duplicate(settings, persona) -> None:
    repository = SimpleNamespace(
        grant_extra_rounds=AsyncMock(
            return_value=OwnerRoundGrant(
                added_rounds=0, used_rounds=5, max_rounds=7, duplicate=True
            )
        )
    )
    cog = ReviewCog(
        SimpleNamespace(user=SimpleNamespace(id=999)),  # type: ignore[arg-type]
        settings,
        _Service(),  # type: ignore[arg-type]
        persona,
        report_owner_run_repository=repository,  # type: ignore[arg-type]
    )
    cog._default_runtime = replace(  # noqa: SLF001
        cog._default_runtime,  # noqa: SLF001
        settings=SimpleNamespace(
            report_owner_quota_admin_user_ids=(123,),
            report_owner_access_max_rounds=5,
        ),
        context_builder=SimpleNamespace(  # type: ignore[arg-type]
            report_owner_for_thread=AsyncMock(return_value=(700, 456))
        ),
    )
    thread = MagicMock(spec=discord.Thread)
    thread.id = 700
    thread.send = AsyncMock()
    context = SimpleNamespace(
        author=SimpleNamespace(id=123),
        guild=SimpleNamespace(id=100),
        channel=thread,
        interaction=SimpleNamespace(id=800),
        defer=AsyncMock(),
        send_followup=AsyncMock(),
    )

    await ReviewCog.grant_report_owner_rounds.callback(cog, context, 2)  # type: ignore[arg-type]

    thread.send.assert_not_awaited()
    assert context.send_followup.await_args.kwargs["ephemeral"] is True


@pytest.mark.asyncio
async def test_grant_report_owner_rounds_warns_if_public_notice_fails(settings, persona) -> None:
    repository = SimpleNamespace(
        grant_extra_rounds=AsyncMock(
            return_value=OwnerRoundGrant(added_rounds=2, used_rounds=5, max_rounds=7)
        )
    )
    cog = ReviewCog(
        SimpleNamespace(user=SimpleNamespace(id=999)),  # type: ignore[arg-type]
        settings,
        _Service(),  # type: ignore[arg-type]
        persona,
        report_owner_run_repository=repository,  # type: ignore[arg-type]
    )
    cog._default_runtime = replace(  # noqa: SLF001
        cog._default_runtime,  # noqa: SLF001
        settings=SimpleNamespace(
            report_owner_quota_admin_user_ids=(123,),
            report_owner_access_max_rounds=5,
        ),
        context_builder=SimpleNamespace(  # type: ignore[arg-type]
            report_owner_for_thread=AsyncMock(return_value=(700, 456))
        ),
    )
    thread = MagicMock(spec=discord.Thread)
    thread.id = 700
    thread.send = AsyncMock(side_effect=RuntimeError("message unavailable"))
    context = SimpleNamespace(
        author=SimpleNamespace(id=123),
        guild=SimpleNamespace(id=100),
        channel=thread,
        interaction=SimpleNamespace(id=800),
        defer=AsyncMock(),
        send_followup=AsyncMock(),
    )

    await ReviewCog.grant_report_owner_rounds.callback(cog, context, 2)  # type: ignore[arg-type]

    repository.grant_extra_rounds.assert_awaited_once()
    assert context.send_followup.await_args.kwargs["ephemeral"] is True
    assert "El cupo se amplió" in context.send_followup.await_args.kwargs["content"]


def _event(settings) -> tuple[object, ReviewTarget, ReviewCase, list[str]]:
    guild_id = min(settings.discord_guild_ids)
    channel_id = min(settings.report_channel_ids)
    owner_id = 123
    replies: list[str] = []

    async def reply(*, content: str, **_kwargs: object) -> None:
        replies.append(content)

    channel = _Channel(channel_id)
    guild = SimpleNamespace(id=guild_id)
    root = SimpleNamespace(
        id=700,
        author=SimpleNamespace(id=owner_id, bot=False),
        webhook_id=None,
        thread=None,
        guild=guild,
    )
    message = SimpleNamespace(
        id=701,
        guild=guild,
        author=SimpleNamespace(id=owner_id, bot=False, roles=()),
        webhook_id=None,
        channel=channel,
        raw_mentions=[999],
        reference=SimpleNamespace(
            type=None,
            resolved=root,
            cached_message=None,
            message_id=root.id,
            channel_id=channel.id,
        ),
        reply=reply,
    )
    target = ReviewTarget(
        root=root,
        trigger=message,
        parent=channel,  # type: ignore[arg-type]
        thread=None,
        response_route=DiscordResponseRoute.THREAD,
    )
    case = ReviewCase(
        guild_id=guild_id,
        parent_channel_id=channel_id,
        channel_id=channel_id,
        thread_id=None,
        root_message_id=root.id,
        trigger_message_id=message.id,
        requester_user_id=owner_id,
        interaction_mode=InteractionMode.INITIAL_REVIEW,
        messages=[],
        attachments=[],
    )
    return message, target, case, replies


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["completed", "failed", "progress_then_failed", "published"])
async def test_owner_reservation_runs_restricted_agent_and_is_finalized(
    settings,
    persona,
    monkeypatch,
    outcome,
) -> None:
    restricted = settings.model_copy(update={"authorized_user_ids_csv": "999999"})
    message, target, case, _ = _event(restricted)
    service = _Service()

    async def review(context, _agent_input):
        service.contexts.append(context)
        context.messages_sent_count = int(outcome in {"completed", "progress_then_failed"})
        context.completed = outcome == "completed"
        context.published = outcome == "published"

    service.review = review
    run_id = uuid4()
    repository = _Repository(
        OwnerRunReservation(
            result=OwnerRunReservationResult.RESERVED,
            used_rounds=3,
            max_rounds=5,
            run_id=run_id,
        )
    )
    bot = SimpleNamespace(user=SimpleNamespace(id=999))
    cog = ReviewCog(
        bot,
        restricted,
        service,  # type: ignore[arg-type]
        persona,
        report_owner_run_repository=repository,  # type: ignore[arg-type]
    )
    builder = _OwnerContextBuilder(target, case)
    cog._default_runtime = replace(  # noqa: SLF001
        cog._default_runtime,  # noqa: SLF001
        settings=SimpleNamespace(report_owner_access_max_rounds=5),
        context_builder=builder,  # type: ignore[arg-type]
    )
    cog.presence = _Presence()  # type: ignore[assignment]
    monkeypatch.setattr(
        "arrest_report_agent.infrastructure.discord.cog.DiscordReviewPublisher",
        _Publisher,
    )

    await cog._handle_message(message)  # type: ignore[arg-type]  # noqa: SLF001

    assert repository.reservations == 1
    successful = outcome in {"completed", "published"}
    assert repository.completed == ([run_id] if successful else [])
    assert repository.failed == ([] if successful else [run_id])
    assert len(service.contexts) == 1
    context = service.contexts[0]
    assert context.access_level is AgentAccessLevel.REPORT_OWNER
    assert context.owner_round == 3
    assert context.owner_max_rounds == 5
    assert context.owner_reservation_result == "reserved"
    assert builder.case_builds == 1


@pytest.mark.asyncio
async def test_structured_initial_review_does_not_reserve_an_owner_round(
    settings,
    persona,
    monkeypatch,
) -> None:
    restricted = settings.model_copy(update={"authorized_user_ids_csv": "999999"})
    message, target, case, _ = _event(restricted)
    service = _Service()
    repository = _Repository(
        OwnerRunReservation(
            result=OwnerRunReservationResult.RESERVED,
            used_rounds=1,
            max_rounds=5,
            run_id=uuid4(),
        )
    )
    cog = ReviewCog(
        SimpleNamespace(user=SimpleNamespace(id=999)),
        restricted,
        service,  # type: ignore[arg-type]
        persona,
        report_owner_run_repository=repository,  # type: ignore[arg-type]
    )
    builder = _OwnerContextBuilder(target, case)
    cog._default_runtime = replace(  # noqa: SLF001
        cog._default_runtime,  # noqa: SLF001
        settings=SimpleNamespace(report_owner_access_max_rounds=5),
        context_builder=builder,  # type: ignore[arg-type]
    )
    cog.presence = _Presence()  # type: ignore[assignment]
    monkeypatch.setattr(
        "arrest_report_agent.infrastructure.discord.cog.DiscordReviewPublisher",
        _Publisher,
    )

    completed = await cog._execute_review(  # noqa: SLF001
        message,  # type: ignore[arg-type]
        target,
        access_level=AgentAccessLevel.REPORT_OWNER,
        consume_owner_round=False,
    )

    assert completed is True
    assert repository.reservations == 0
    assert repository.completed == []
    assert repository.failed == []
    assert len(service.contexts) == 1
    assert service.contexts[0].owner_round is None


@pytest.mark.asyncio
async def test_exhausted_owner_gets_specific_notice_without_running_agent(
    settings,
    persona,
    monkeypatch,
) -> None:
    restricted = settings.model_copy(update={"authorized_user_ids_csv": "999999"})
    message, target, case, replies = _event(restricted)
    service = _Service()
    repository = _Repository(
        OwnerRunReservation(
            result=OwnerRunReservationResult.EXHAUSTED,
            used_rounds=5,
            max_rounds=5,
        )
    )
    bot = SimpleNamespace(user=SimpleNamespace(id=999))
    cog = ReviewCog(
        bot,
        restricted,
        service,  # type: ignore[arg-type]
        persona,
        report_owner_run_repository=repository,  # type: ignore[arg-type]
    )
    cog._default_runtime = replace(  # noqa: SLF001
        cog._default_runtime,  # noqa: SLF001
        settings=SimpleNamespace(report_owner_access_max_rounds=5),
        context_builder=_OwnerContextBuilder(target, case),  # type: ignore[arg-type]
    )
    cog.presence = _Presence()  # type: ignore[assignment]
    cog._notify_owner_quota_exhausted = AsyncMock()  # type: ignore[method-assign]  # noqa: SLF001
    monkeypatch.setattr(
        "arrest_report_agent.infrastructure.discord.cog.DiscordReviewPublisher",
        _Publisher,
    )

    await cog._handle_message(message)  # type: ignore[arg-type]  # noqa: SLF001

    assert repository.reservations == 1
    assert service.contexts == []
    assert len(replies) == 1
    assert "5" in replies[0]
    assert "habilitación" not in replies[0]
    cog._notify_owner_quota_exhausted.assert_awaited_once_with(  # type: ignore[attr-defined]  # noqa: SLF001
        guild=message.guild,
        root_message_id=case.root_message_id,
        owner_user_id=case.requester_user_id,
        thread_id=case.thread_id,
        base_max_rounds=5,
    )


@pytest.mark.asyncio
async def test_last_owner_round_triggers_staff_notification(settings, persona, monkeypatch) -> None:
    restricted = settings.model_copy(update={"authorized_user_ids_csv": "999999"})
    message, target, case, _ = _event(restricted)
    service = _Service()
    repository = _Repository(
        OwnerRunReservation(
            result=OwnerRunReservationResult.RESERVED,
            used_rounds=5,
            max_rounds=5,
            run_id=uuid4(),
        )
    )
    cog = ReviewCog(
        SimpleNamespace(user=SimpleNamespace(id=999)),
        restricted,
        service,  # type: ignore[arg-type]
        persona,
        report_owner_run_repository=repository,  # type: ignore[arg-type]
    )
    cog._default_runtime = replace(  # noqa: SLF001
        cog._default_runtime,  # noqa: SLF001
        settings=SimpleNamespace(report_owner_access_max_rounds=5),
        context_builder=_OwnerContextBuilder(target, case),  # type: ignore[arg-type]
    )
    cog.presence = _Presence()  # type: ignore[assignment]
    cog._notify_owner_quota_exhausted = AsyncMock()  # type: ignore[method-assign]  # noqa: SLF001
    monkeypatch.setattr(
        "arrest_report_agent.infrastructure.discord.cog.DiscordReviewPublisher", _Publisher
    )

    await cog._handle_message(message)  # type: ignore[arg-type]  # noqa: SLF001

    assert repository.reservations == 1
    assert len(service.contexts) == 1
    cog._notify_owner_quota_exhausted.assert_awaited_once_with(  # type: ignore[attr-defined]  # noqa: SLF001
        guild=message.guild,
        root_message_id=case.root_message_id,
        owner_user_id=case.requester_user_id,
        thread_id=case.thread_id,
        base_max_rounds=5,
    )


@pytest.mark.asyncio
async def test_quota_alert_mentions_both_roles_and_links_to_thread(settings, persona) -> None:
    repository = SimpleNamespace(
        claim_exhaustion_notification=AsyncMock(side_effect=[5, None]),
        mark_exhaustion_notification_sent=AsyncMock(),
        release_exhaustion_notification=AsyncMock(),
    )
    cog = ReviewCog(
        SimpleNamespace(user=SimpleNamespace(id=999)),  # type: ignore[arg-type]
        settings,
        _Service(),  # type: ignore[arg-type]
        persona,
        report_owner_run_repository=repository,  # type: ignore[arg-type]
    )
    cog._default_runtime = replace(  # noqa: SLF001
        cog._default_runtime,  # noqa: SLF001
        settings=SimpleNamespace(
            report_owner_quota_grant_role_ids=(200, 201),
            report_owner_quota_exhausted_notification_channel_id=300,
        ),
    )
    channel = MagicMock(spec=discord.TextChannel)
    channel.permissions_for.return_value = SimpleNamespace(
        view_channel=True, send_messages=True, embed_links=True, mention_everyone=False
    )
    channel.send = AsyncMock(return_value=SimpleNamespace(id=400))
    guild = SimpleNamespace(
        id=100,
        me=SimpleNamespace(id=999),
        get_channel=lambda channel_id: channel if channel_id == 300 else None,
        get_role=lambda role_id: SimpleNamespace(id=role_id, mentionable=True),
    )

    for _ in range(2):
        await cog._notify_owner_quota_exhausted(  # noqa: SLF001
            guild=guild,  # type: ignore[arg-type]
            root_message_id=700,
            owner_user_id=123,
            thread_id=701,
            base_max_rounds=5,
        )

    channel.send.assert_awaited_once()
    kwargs = channel.send.await_args.kwargs
    assert kwargs["content"] == "<@&200> <@&201>"
    assert "https://discord.com/channels/100/701" in kwargs["embed"].description
    assert "<@123>" in kwargs["embed"].description
    assert kwargs["allowed_mentions"].users is False
    assert len(kwargs["allowed_mentions"].roles) == 2
    repository.mark_exhaustion_notification_sent.assert_awaited_once_with(
        guild_id=100,
        root_message_id=700,
        owner_user_id=123,
        max_rounds=5,
        message_id=400,
    )
    repository.release_exhaustion_notification.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_quota_alert_can_be_retried(settings, persona) -> None:
    repository = SimpleNamespace(
        claim_exhaustion_notification=AsyncMock(return_value=5),
        mark_exhaustion_notification_sent=AsyncMock(),
        release_exhaustion_notification=AsyncMock(),
    )
    cog = ReviewCog(
        SimpleNamespace(user=SimpleNamespace(id=999)),  # type: ignore[arg-type]
        settings,
        _Service(),  # type: ignore[arg-type]
        persona,
        report_owner_run_repository=repository,  # type: ignore[arg-type]
    )
    cog._default_runtime = replace(  # noqa: SLF001
        cog._default_runtime,  # noqa: SLF001
        settings=SimpleNamespace(
            report_owner_quota_grant_role_ids=(200, 201),
            report_owner_quota_exhausted_notification_channel_id=300,
        ),
    )
    channel = MagicMock(spec=discord.TextChannel)
    channel.permissions_for.return_value = SimpleNamespace(
        view_channel=True, send_messages=True, embed_links=True, mention_everyone=True
    )
    channel.send = AsyncMock(side_effect=RuntimeError("Discord unavailable"))
    guild = SimpleNamespace(
        id=100,
        me=SimpleNamespace(id=999),
        get_channel=lambda _channel_id: channel,
        get_role=lambda role_id: SimpleNamespace(id=role_id, mentionable=False),
    )

    await cog._notify_owner_quota_exhausted(  # noqa: SLF001
        guild=guild,  # type: ignore[arg-type]
        root_message_id=700,
        owner_user_id=123,
        thread_id=701,
        base_max_rounds=5,
    )

    repository.release_exhaustion_notification.assert_awaited_once_with(
        guild_id=100,
        root_message_id=700,
        owner_user_id=123,
        max_rounds=5,
    )
    repository.mark_exhaustion_notification_sent.assert_not_awaited()


def test_staff_quota_alert_uses_readable_thread_link() -> None:
    embed = build_owner_quota_exhausted_staff_embed(
        guild_id=100, thread_id=701, owner_user_id=123, max_rounds=7
    )
    assert embed.title == "🎟️ Turnos de revisión agotados"
    assert "límite de 7 turnos" in embed.description
    assert "[📋 Abrir el hilo de revisión](https://discord.com/channels/100/701)" in embed.description
    assert "usa `/ampliar-cupo` para concederle de 1 a 10 turnos adicionales" in embed.description


@pytest.mark.asyncio
async def test_owner_context_failure_does_not_reserve_a_round(
    settings,
    persona,
    monkeypatch,
) -> None:
    restricted = settings.model_copy(update={"authorized_user_ids_csv": "999999"})
    message, target, case, _ = _event(restricted)
    service = _Service()
    repository = _Repository(
        OwnerRunReservation(
            result=OwnerRunReservationResult.RESERVED,
            used_rounds=1,
            max_rounds=5,
            run_id=uuid4(),
        )
    )
    cog = ReviewCog(
        SimpleNamespace(user=SimpleNamespace(id=999)),
        restricted,
        service,  # type: ignore[arg-type]
        persona,
        report_owner_run_repository=repository,  # type: ignore[arg-type]
    )
    cog._default_runtime = replace(  # noqa: SLF001
        cog._default_runtime,  # noqa: SLF001
        settings=SimpleNamespace(report_owner_access_max_rounds=5),
        context_builder=_FailingOwnerContextBuilder(target, case),  # type: ignore[arg-type]
    )
    cog.presence = _Presence()  # type: ignore[assignment]
    monkeypatch.setattr(
        "arrest_report_agent.infrastructure.discord.cog.DiscordReviewPublisher",
        _Publisher,
    )

    await cog._handle_message(message)  # type: ignore[arg-type]  # noqa: SLF001

    assert repository.reservations == 0
    assert service.contexts == []


@pytest.mark.asyncio
async def test_invalid_owner_report_gets_format_notice_without_reserving_or_running(
    settings,
    persona,
) -> None:
    restricted = settings.model_copy(update={"authorized_user_ids_csv": "999999"})
    message, target, case, replies = _event(restricted)
    service = _Service()
    repository = _Repository(
        OwnerRunReservation(
            result=OwnerRunReservationResult.RESERVED,
            used_rounds=1,
            max_rounds=5,
            run_id=uuid4(),
        )
    )
    cog = ReviewCog(
        SimpleNamespace(user=SimpleNamespace(id=999, mention="<@999>")),
        restricted,
        service,  # type: ignore[arg-type]
        persona,
        report_owner_run_repository=repository,  # type: ignore[arg-type]
    )
    cog._default_runtime = replace(  # noqa: SLF001
        cog._default_runtime,  # noqa: SLF001
        settings=SimpleNamespace(report_owner_access_max_rounds=5),
        context_builder=_InvalidOwnerContextBuilder(target, case),  # type: ignore[arg-type]
    )

    await cog._handle_message(message)  # type: ignore[arg-type]  # noqa: SLF001

    assert repository.reservations == 0
    assert service.contexts == []
    assert len(replies) == 1
    assert "<@999> Nombre Apellido" in replies[0]
    assert "4" in replies[0]

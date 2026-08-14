from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

from arrest_report_agent.domain import AgentAccessLevel, InteractionMode, ReviewCase
from arrest_report_agent.domain.errors import ReportOwnerSubmissionError
from arrest_report_agent.infrastructure.discord.cog import ReviewCog
from arrest_report_agent.infrastructure.discord.context import (
    DiscordResponseRoute,
    ReviewTarget,
)
from arrest_report_agent.report_owner_access import (
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
        yield SimpleNamespace(promote=lambda *_args: None)


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
        context.interaction_completed = True


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
async def test_owner_reservation_runs_restricted_agent_and_is_finalized(
    settings,
    persona,
    monkeypatch,
) -> None:
    restricted = settings.model_copy(update={"authorized_user_ids_csv": "999999"})
    message, target, case, _ = _event(restricted)
    service = _Service()
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
    assert repository.completed == [run_id]
    assert repository.failed == []
    assert len(service.contexts) == 1
    context = service.contexts[0]
    assert context.access_level is AgentAccessLevel.REPORT_OWNER
    assert context.owner_round == 3
    assert context.owner_max_rounds == 5
    assert context.owner_reservation_result == "reserved"
    assert builder.case_builds == 1


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

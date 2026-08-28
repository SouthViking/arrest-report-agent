from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from arrest_report_agent.application.ports import PollAnswerDraft, PollCreateRequest
from arrest_report_agent.polls import DiscordPollService, PollRecord
from arrest_report_agent.polls.service import _poll_emoji


class FakePollRepository:
    def __init__(self, record: PollRecord | None = None) -> None:
        self.record = record
        self.created: list[PollRecord] = []
        self.synced: list[tuple[int, int]] = []
        self.ended: list[tuple[int, int]] = []
        self.deleted: list[tuple[int, int]] = []

    async def create(self, record: PollRecord) -> PollRecord:
        self.created.append(record)
        self.record = record
        return record

    async def get(self, guild_id: int, message_id: int) -> PollRecord | None:
        if self.record is None:
            return None
        if self.record.guild_id != guild_id or self.record.message_id != message_id:
            return None
        return self.record

    async def mark_synced(self, guild_id: int, message_id: int, *, at: datetime) -> None:
        self.synced.append((guild_id, message_id))

    async def mark_ended(self, guild_id: int, message_id: int, *, at: datetime) -> None:
        self.ended.append((guild_id, message_id))

    async def mark_deleted(self, guild_id: int, message_id: int, *, at: datetime) -> None:
        self.deleted.append((guild_id, message_id))


def _record() -> PollRecord:
    created = datetime(2026, 8, 27, 12, tzinfo=UTC)
    return PollRecord(
        message_id=33333333333333333,
        guild_id=11111111111111111,
        channel_id=22222222222222222,
        creator_user_id=44444444444444444,
        created_at=created,
        expires_at=created + timedelta(hours=24),
    )


@pytest.mark.asyncio
async def test_create_poll_publishes_native_poll_and_persists_only_metadata(monkeypatch) -> None:
    repository = FakePollRepository()
    service = DiscordPollService(SimpleNamespace(), repository)  # type: ignore[arg-type]
    message = SimpleNamespace(
        id=33333333333333333,
        jump_url="https://discord.com/channels/1/2/3",
        created_at=datetime(2026, 8, 27, 12, tzinfo=UTC),
        poll=None,
        delete=AsyncMock(),
    )
    channel = SimpleNamespace(id=22222222222222222, send=AsyncMock(return_value=message))
    monkeypatch.setattr(
        service,
        "_resolve_channel",
        AsyncMock(return_value=(SimpleNamespace(), channel, SimpleNamespace())),
    )

    result = await service.create(
        guild_id=11111111111111111,
        current_channel_id=22222222222222222,
        requester_user_id=44444444444444444,
        target_channel_id=None,
        request=PollCreateRequest(
            question="¿Qué turno prefieren?",
            answers=(
                PollAnswerDraft("Mañana", "🌅"),
                PollAnswerDraft("Noche", "<:night:12345678901234567>"),
            ),
            duration_hours=48,
            allow_multiselect=True,
            silent=True,
            intro="Marquen todos los que les sirvan.",
        ),
    )

    sent = channel.send.await_args.kwargs
    assert sent["poll"].question.text == "¿Qué turno prefieren?"
    assert sent["poll"].allow_multiselect is True
    assert sent["poll"].duration == 48
    assert sent["content"] == "Marquen todos los que les sirvan."
    assert sent["silent"] is True
    assert result["ok"] is True
    assert repository.created[0].message_id == message.id
    assert set(repository.created[0].model_dump()) == {
        "message_id",
        "guild_id",
        "channel_id",
        "creator_user_id",
        "created_at",
        "expires_at",
        "ended_at",
        "deleted_at",
        "last_synced_at",
        "last_activity_at",
    }


@pytest.mark.asyncio
async def test_get_poll_uses_live_discord_results_and_labels_multiselect_percentages(
    monkeypatch,
) -> None:
    record = _record()
    repository = FakePollRepository(record)
    service = DiscordPollService(SimpleNamespace(), repository)  # type: ignore[arg-type]
    answers = [
        SimpleNamespace(id=1, text="Mañana", emoji="🌅", count=3),
        SimpleNamespace(id=2, text="Noche", emoji="🌙", count=1),
    ]
    poll = SimpleNamespace(
        question=SimpleNamespace(text="¿Qué turno prefieren?"),
        allow_multiselect=True,
        answers=answers,
        expiry=record.expires_at,
        results=SimpleNamespace(is_finalized=False),
        total_votes=lambda: 4,
        has_ended=lambda: False,
        get_answer=lambda answer_id: next(
            (answer for answer in answers if answer.id == answer_id),
            None,
        ),
    )
    message = SimpleNamespace(
        id=record.message_id,
        jump_url="https://discord.com/channels/1/2/3",
        poll=poll,
    )
    monkeypatch.setattr(
        service,
        "_resolve_channel",
        AsyncMock(return_value=(SimpleNamespace(), SimpleNamespace(), SimpleNamespace())),
    )
    monkeypatch.setattr(service, "_fetch_message", AsyncMock(return_value=message))

    result = await service.get(
        guild_id=record.guild_id,
        requester_user_id=record.creator_user_id,
        message_id=record.message_id,
        include_voters=False,
        answer_ids=(),
        voter_limit_per_answer=25,
        after_user_id=None,
    )

    payload = result["poll"]
    assert isinstance(payload, dict)
    assert payload["question"] == "¿Qué turno prefieren?"
    assert payload["total_participants"] is None
    assert payload["total_selections"] == 4
    first = payload["answers"][0]
    assert first["percentage"] == 75.0
    assert first["percentage_basis"] == "all_selections"
    assert repository.synced == [(record.guild_id, record.message_id)]


def test_custom_poll_emoji_is_parsed_as_a_discord_partial_emoji() -> None:
    emoji = _poll_emoji("<:night:12345678901234567>")

    assert emoji is not None
    assert str(emoji) == "<:night:12345678901234567>"
    assert _poll_emoji("🌙") == "🌙"

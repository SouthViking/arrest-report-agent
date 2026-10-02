from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from arrest_report_agent.application.conversations import ConversationKey
from arrest_report_agent.domain import AgentAccessLevel, InteractionMode, ReviewCase
from arrest_report_agent.infrastructure.discord.cog import ReviewCog
from arrest_report_agent.infrastructure.discord.context import ReviewTarget


@asynccontextmanager
async def no_context():
    yield None


def event(settings, message_id, *, channel_id=None, mention=True, bot=False, user_id=30):
    return SimpleNamespace(
        id=message_id,
        guild=SimpleNamespace(id=min(settings.discord_guild_ids)),
        channel=SimpleNamespace(
            id=channel_id or min(settings.report_channel_ids),
            typing=no_context,
        ),
        author=SimpleNamespace(id=user_id, display_name="Persona", bot=bot, roles=()),
        webhook_id=None,
        type=discord.MessageType.default,
        reference=None,
        content="Corrijo mi petición",
        raw_mentions=[999] if mention else [],
        attachments=[],
        embeds=[],
        reply=AsyncMock(),
    )


def make_cog(settings, persona, *, thread=False, consume=None):
    started, finish = asyncio.Event(), asyncio.Event()
    contexts = []

    async def review(context, _input, **_kwargs):
        contexts.append(context)
        started.set()
        await finish.wait()
        if consume is not None:
            await consume(context)
        context.completed = True
        context.messages_sent_count = 1

    service = SimpleNamespace(
        review=review,
        model=SimpleNamespace(
            id="gpt-test",
            reasoning_effort="low",
            runtime_defaults=SimpleNamespace(image_detail="auto"),
        ),
    )
    cog = ReviewCog(SimpleNamespace(user=SimpleNamespace(id=999)), settings, service, persona)
    cog.presence = SimpleNamespace(handling=no_context)

    async def resolve(message, _activation):
        root = SimpleNamespace(id=10, guild=message.guild)
        return ReviewTarget(
            root=root if thread else message,
            trigger=message,
            parent=message.channel,
            thread=message.channel if thread else None,
        )

    async def build(target, **_kwargs):
        return ReviewCase(
            guild_id=target.trigger.guild.id,
            parent_channel_id=target.parent.id,
            channel_id=target.trigger.channel.id,
            thread_id=target.thread.id if target.thread else None,
            root_message_id=target.root.id,
            trigger_message_id=target.trigger.id,
            requester_user_id=target.trigger.author.id,
            interaction_mode=InteractionMode.THREAD_FOLLOW_UP if thread else InteractionMode.INITIAL_REVIEW,
            messages=[],
            attachments=[],
        )

    builder = SimpleNamespace(
        resolve_target=AsyncMock(side_effect=resolve), build_case=AsyncMock(side_effect=build)
    )
    cog._default_runtime = replace(cog._default_runtime, context_builder=builder)
    return cog, started, finish, contexts


@pytest.mark.asyncio
@pytest.mark.parametrize("thread", [False, True])
async def test_mentions_and_plain_messages_join_single_active_run(settings, persona, thread):
    cog, started, finish, contexts = make_cog(settings, persona, thread=thread)
    initial = event(settings, 100)
    task = asyncio.create_task(cog.on_message(initial))
    try:
        await asyncio.wait_for(started.wait(), 2)
        await cog.on_message(event(settings, 101, mention=False))
        await cog.on_message(event(settings, 102))
        await cog.on_message(event(settings, 102))  # Gateway redelivery.
        await cog.on_message(event(settings, 103, bot=True))
        assert len(contexts) == 1
        cog.context_builder.resolve_target.assert_awaited_once()
        live = contexts[0].live_conversation
        pending = await live.store.drain(live.lease)
        assert [item.id for item in pending] == [101, 102]
        assert contexts[0].access_level is AgentAccessLevel.FULL
    finally:
        finish.set()
        await task
    assert await cog._conversations.acquire(ConversationKey(initial.guild.id, initial.channel.id), 104)


@pytest.mark.asyncio
async def test_channels_run_independently_and_unmentioned_idle_messages_do_not_start(settings, persona):
    cog, started, finish, contexts = make_cog(settings, persona)
    await cog.on_message(event(settings, 99, mention=False))
    assert not contexts
    first = asyncio.create_task(cog.on_message(event(settings, 100)))
    await asyncio.wait_for(started.wait(), 2)
    started.clear()
    other = asyncio.create_task(cog.on_message(event(settings, 101, channel_id=777)))
    await asyncio.wait_for(started.wait(), 2)
    try:
        assert len(contexts) == 2
        await cog.on_message(event(settings, 102, channel_id=777, mention=False))
        assert not await contexts[0].live_conversation.store.has_pending(contexts[0].live_conversation.lease)
        assert await contexts[1].live_conversation.store.has_pending(contexts[1].live_conversation.lease)
    finally:
        finish.set()
        await asyncio.gather(first, other)


@pytest.mark.asyncio
async def test_cancellation_releases_lease(settings, persona):
    cog, started, finish, contexts = make_cog(settings, persona)
    initial = event(settings, 100)
    task = asyncio.create_task(cog.on_message(initial))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await cog._conversations.acquire(ConversationKey(initial.guild.id, initial.channel.id), 101)


@pytest.mark.asyncio
async def test_unauthorized_messages_are_context_without_promoting_the_run(settings, persona):
    restricted = settings.model_copy(update={"authorized_user_ids_csv": "30"})
    cog, started, finish, contexts = make_cog(restricted, persona)
    task = asyncio.create_task(cog.on_message(event(settings, 100)))
    await asyncio.wait_for(started.wait(), 2)
    try:
        incoming = event(settings, 101, user_id=31)
        await cog.on_message(incoming)
        pending = await contexts[0].live_conversation.store.drain(contexts[0].live_conversation.lease)
        assert len(pending) == 1
        assert '"authorized_staff":false' in pending[0].text
        assert contexts[0].case.requester_user_id == 30
        incoming.reply.assert_not_called()
    finally:
        finish.set()
        await task

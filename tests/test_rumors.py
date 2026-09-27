from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from pydantic import ValidationError

from arrest_report_agent.config import RumorOptions
from arrest_report_agent.rumors import RumorCog, RumorModal, build_rumor_embed
from arrest_report_agent.rumors.discord import normalize_message

GUILD_ID = 1234674521416663070
GNT_CHANNEL_ID = 1234710666548477952
DLM_CHANNEL_ID = 1258640770504593501
GNT_ROLE_ID = 1258627732594495572
DLM_ROLE_ID = 1258630594741665893
AUTHOR_ID = 42


def _options() -> RumorOptions:
    return RumorOptions.model_validate(
        {
            "channels": [
                {"channel_id": GNT_CHANNEL_ID, "role_id": GNT_ROLE_ID},
                {"channel_id": DLM_CHANNEL_ID, "role_id": DLM_ROLE_ID},
            ]
        }
    )


def _setup(channel_id: int, *, options: RumorOptions | None = None) -> SimpleNamespace:
    role_id = GNT_ROLE_ID if channel_id == GNT_CHANNEL_ID else DLM_ROLE_ID
    role = SimpleNamespace(id=role_id, mention=f"<@&{role_id}>", mentionable=False)
    member = MagicMock(spec=discord.Member)
    member.id = AUTHOR_ID
    bot_member = MagicMock(spec=discord.Member)
    guild = SimpleNamespace(
        id=GUILD_ID,
        me=bot_member,
        get_role=lambda requested: role if requested == role_id else None,
    )
    channel = MagicMock(spec=discord.TextChannel)
    channel.id = channel_id
    channel.guild = guild
    channel.permissions_for.return_value = SimpleNamespace(
        view_channel=True,
        send_messages=True,
        embed_links=True,
        mention_everyone=True,
    )
    channel.send = AsyncMock(return_value=SimpleNamespace(id=12345))
    provider = SimpleNamespace(
        cached=AsyncMock(
            return_value=SimpleNamespace(settings=SimpleNamespace(rumor_options=options or _options()))
        )
    )
    repository = SimpleNamespace(reserve=AsyncMock(return_value=True), mark_published=AsyncMock())
    cog = RumorCog(provider, repository)
    return SimpleNamespace(
        role=role, member=member, guild=guild, channel=channel,
        provider=provider, repository=repository, cog=cog,
    )


def _interaction(state: SimpleNamespace, *, interaction_id: int = 100) -> SimpleNamespace:
    return SimpleNamespace(
        id=interaction_id,
        guild=state.guild,
        channel=state.channel,
        user=state.member,
        response=SimpleNamespace(
            defer=AsyncMock(), send_message=AsyncMock(), is_done=lambda: False
        ),
        followup=SimpleNamespace(send=AsyncMock()),
        delete_original_response=AsyncMock(),
    )


def test_channel_role_mapping_is_data_driven_and_unique() -> None:
    options = _options()
    assert options.target_for_channel(GNT_CHANNEL_ID).role_id == GNT_ROLE_ID
    assert options.target_for_channel(DLM_CHANNEL_ID).role_id == DLM_ROLE_ID
    assert options.target_for_channel(999) is None
    with pytest.raises(ValidationError, match="rumor channels must be unique"):
        RumorOptions.model_validate(
            {"channels": [
                {"channel_id": GNT_CHANNEL_ID, "role_id": GNT_ROLE_ID},
                {"channel_id": GNT_CHANNEL_ID, "role_id": DLM_ROLE_ID},
            ]}
        )


def test_message_validation_and_embed_do_not_expose_author_or_ping_others() -> None:
    assert normalize_message("  Un rumor creíble  ") == "Un rumor creíble"
    with pytest.raises(ValueError, match="entre 10 y 1500"):
        normalize_message("  \t  ")
    embed = build_rumor_embed("Se vio a <@123456789012345678> en la estación.")
    assert embed.title is None
    assert embed.author is None
    assert embed.footer is None
    assert "<@123456789012345678>" not in embed.description


@pytest.mark.asyncio
@pytest.mark.parametrize("channel_id", [GNT_CHANNEL_ID, DLM_CHANNEL_ID])
async def test_rumor_uses_one_modal_and_publication_flow_for_both_channels(channel_id: int) -> None:
    state = _setup(channel_id)
    context = SimpleNamespace(
        guild=state.guild,
        channel=state.channel,
        author=state.member,
        send_modal=AsyncMock(),
        respond=AsyncMock(),
    )
    await RumorCog.rumor.callback(state.cog, context)
    modal = context.send_modal.await_args.args[0]
    assert isinstance(modal, RumorModal)
    assert len(modal.children) == 2
    assert "sanciones" in modal.children[0].content
    assert "anónima" in modal.children[0].content
    assert modal.role_id == state.role.id

    interaction = _interaction(state)
    await state.cog.publish(
        interaction,
        requester_id=AUTHOR_ID,
        guild_id=GUILD_ID,
        channel_id=channel_id,
        role_id=state.role.id,
        raw_message="Hay una noticia en la estación.",
    )
    state.repository.reserve.assert_awaited_once()
    assert state.repository.reserve.await_args.kwargs["author_user_id"] == AUTHOR_ID
    sent = state.channel.send.await_args.kwargs
    assert sent["content"] == state.role.mention
    assert sent["embed"].title is None
    assert sent["allowed_mentions"].everyone is False
    assert sent["allowed_mentions"].users is False
    assert [role.id for role in sent["allowed_mentions"].roles] == [state.role.id]
    state.repository.mark_published.assert_awaited_once_with(100, 12345)
    interaction.delete_original_response.assert_awaited_once()
    context.respond.assert_not_awaited()


@pytest.mark.asyncio
async def test_unconfigured_channel_cannot_open_modal() -> None:
    state = _setup(999)
    context = SimpleNamespace(
        guild=state.guild,
        channel=state.channel,
        author=state.member,
        send_modal=AsyncMock(),
        respond=AsyncMock(),
    )
    await RumorCog.rumor.callback(state.cog, context)
    context.send_modal.assert_not_awaited()
    assert context.respond.await_args.kwargs["ephemeral"] is True


@pytest.mark.asyncio
async def test_member_without_channel_access_cannot_open_modal() -> None:
    state = _setup(GNT_CHANNEL_ID)
    state.channel.permissions_for.return_value.view_channel = False
    context = SimpleNamespace(
        guild=state.guild,
        channel=state.channel,
        author=state.member,
        send_modal=AsyncMock(),
        respond=AsyncMock(),
    )
    await RumorCog.rumor.callback(state.cog, context)
    context.send_modal.assert_not_awaited()
    assert context.respond.await_args.kwargs["ephemeral"] is True


@pytest.mark.asyncio
async def test_modal_rechecks_target_and_does_not_publish_after_role_change() -> None:
    state = _setup(GNT_CHANNEL_ID)
    interaction = _interaction(state)
    await state.cog.publish(
        interaction,
        requester_id=AUTHOR_ID,
        guild_id=GUILD_ID,
        channel_id=GNT_CHANNEL_ID,
        role_id=DLM_ROLE_ID,
        raw_message="Hay una noticia en la estación.",
    )
    state.channel.send.assert_not_awaited()
    state.repository.reserve.assert_not_awaited()
    interaction.response.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_duplicate_submission_does_not_send_twice() -> None:
    state = _setup(GNT_CHANNEL_ID)
    state.repository.reserve.return_value = False
    interaction = _interaction(state)
    await state.cog.publish(
        interaction,
        requester_id=AUTHOR_ID,
        guild_id=GUILD_ID,
        channel_id=GNT_CHANNEL_ID,
        role_id=GNT_ROLE_ID,
        raw_message="Hay una noticia en la estación.",
    )
    state.channel.send.assert_not_awaited()
    interaction.delete_original_response.assert_awaited_once()

from __future__ import annotations

from types import SimpleNamespace

from arrest_report_agent.infrastructure.discord import trigger
from arrest_report_agent.infrastructure.discord.trigger import (
    DiscordAccessPolicy,
    TriggerActivation,
    TriggerDecision,
    classify_message,
)


def make_message(
    settings,
    *,
    mentions: list[int],
    user_id: int = 123,
    bot: bool = False,
    webhook_id=None,
    channel_id=None,
    role_ids: tuple[int, ...] = (),
):
    configured_guilds = settings.discord_guild_ids
    return SimpleNamespace(
        guild=SimpleNamespace(id=min(configured_guilds) if configured_guilds else 1),
        author=SimpleNamespace(
            id=user_id,
            bot=bot,
            roles=[SimpleNamespace(id=role_id) for role_id in role_ids],
        ),
        webhook_id=webhook_id,
        channel=SimpleNamespace(id=channel_id or 2),
        raw_mentions=mentions,
        reference=None,
    )


def test_explicit_mention_anywhere_triggers(settings) -> None:
    message = make_message(settings, mentions=[999])
    message.content = "Terry Parker — por favor <@999> revisa este reporte"

    assert classify_message(message, 999, settings) is TriggerDecision.REVIEW


def test_central_policy_receives_access_scopes(settings) -> None:
    policy = DiscordAccessPolicy.from_settings(settings)

    assert policy.guilds.ids == settings.discord_guild_ids
    assert policy.users.ids == settings.authorized_user_ids
    assert policy.roles.ids == frozenset()


def test_unmentioned_message_is_ignored(settings) -> None:
    assert (
        classify_message(make_message(settings, mentions=[]), 999, settings)
        is TriggerDecision.IGNORE_NOT_ADDRESSED
    )


def test_resolved_reply_to_bot_triggers_without_mention(settings) -> None:
    message = make_message(settings, mentions=[])
    message.reference = SimpleNamespace(
        type=trigger.discord.MessageReferenceType.default,
        resolved=SimpleNamespace(author=SimpleNamespace(id=999)),
        cached_message=None,
    )

    assert classify_message(message, 999, settings) is TriggerDecision.REVIEW


def test_reply_to_another_user_is_ignored_without_mention(settings) -> None:
    message = make_message(settings, mentions=[])
    message.reference = SimpleNamespace(
        type=trigger.discord.MessageReferenceType.default,
        resolved=SimpleNamespace(author=SimpleNamespace(id=555)),
        cached_message=None,
    )

    assert classify_message(message, 999, settings) is TriggerDecision.IGNORE_NOT_ADDRESSED


def test_forwarded_message_is_not_treated_as_a_reply(settings) -> None:
    message = make_message(settings, mentions=[])
    message.reference = SimpleNamespace(
        type=trigger.discord.MessageReferenceType.forward,
        resolved=SimpleNamespace(author=SimpleNamespace(id=999)),
        cached_message=None,
    )

    assert classify_message(message, 999, settings) is TriggerDecision.IGNORE_NOT_ADDRESSED


async def test_uncached_reply_to_bot_is_resolved_before_classification(settings) -> None:
    fetched_ids: list[int] = []

    async def fetch_message(message_id: int):
        fetched_ids.append(message_id)
        return SimpleNamespace(author=SimpleNamespace(id=999))

    message = make_message(settings, mentions=[])
    message.channel.fetch_message = fetch_message
    message.reference = SimpleNamespace(
        type=trigger.discord.MessageReferenceType.default,
        message_id=456,
        channel_id=message.channel.id,
        resolved=None,
        cached_message=None,
    )

    decision = await DiscordAccessPolicy.from_settings(settings).classify_event(message, 999)

    assert decision is TriggerDecision.REVIEW
    assert fetched_ids == [456]


async def test_event_evaluation_retains_routing_activation(settings) -> None:
    policy = DiscordAccessPolicy.from_settings(settings)
    mention = make_message(settings, mentions=[999])
    reply = make_message(settings, mentions=[])
    reply.reference = SimpleNamespace(
        type=trigger.discord.MessageReferenceType.default,
        resolved=SimpleNamespace(author=SimpleNamespace(id=999)),
        cached_message=None,
    )

    mention_evaluation = await policy.evaluate_event(mention, 999)
    reply_evaluation = await policy.evaluate_event(reply, 999)

    assert mention_evaluation.decision is TriggerDecision.REVIEW
    assert mention_evaluation.activation is TriggerActivation.MENTION
    assert reply_evaluation.decision is TriggerDecision.REVIEW
    assert reply_evaluation.activation is TriggerActivation.BOT_REPLY


def test_bot_and_webhook_are_ignored_but_any_human_channel_triggers(settings) -> None:
    assert (
        classify_message(make_message(settings, mentions=[999], bot=True), 999, settings)
        is TriggerDecision.IGNORE_BOT_OR_WEBHOOK
    )
    assert (
        classify_message(make_message(settings, mentions=[999], webhook_id=123), 999, settings)
        is TriggerDecision.IGNORE_BOT_OR_WEBHOOK
    )
    assert (
        classify_message(make_message(settings, mentions=[999], channel_id=555), 999, settings)
        is TriggerDecision.REVIEW
    )


def test_any_child_thread_triggers(settings) -> None:
    message = make_message(settings, mentions=[999])
    message.channel = SimpleNamespace(id=777, parent_id=555)

    assert classify_message(message, 999, settings) is TriggerDecision.REVIEW


def test_unauthorized_user_is_stopped_in_every_channel(settings) -> None:
    restricted = settings.model_copy(update={"authorized_user_ids_csv": "321"})

    assert (
        classify_message(
            make_message(restricted, mentions=[999], user_id=123),
            999,
            restricted,
        )
        is TriggerDecision.REDIRECT_UNAUTHORIZED_USER
    )
    assert (
        classify_message(
            make_message(restricted, mentions=[999], user_id=123, channel_id=555),
            999,
            restricted,
        )
        is TriggerDecision.REDIRECT_UNAUTHORIZED_USER
    )


def test_authorized_user_can_reach_review(settings) -> None:
    restricted = settings.model_copy(update={"authorized_user_ids_csv": "123,321"})

    assert (
        classify_message(
            make_message(restricted, mentions=[999], user_id=123),
            999,
            restricted,
        )
        is TriggerDecision.REVIEW
    )


def test_agent_user_and_role_policies_are_combined_with_or(settings) -> None:
    policy = DiscordAccessPolicy(
        guilds=settings.guild_allowlist,
        users=trigger.SnowflakeAllowlist(ids=frozenset({123})),
        roles=trigger.SnowflakeAllowlist(ids=frozenset({777})),
    )

    assert policy.classify(
        make_message(settings, mentions=[999], user_id=123, role_ids=(777,)),
        999,
    ) is TriggerDecision.REVIEW
    assert (
        policy.classify(
            make_message(settings, mentions=[999], user_id=456, role_ids=(777,)),
            999,
        )
        is TriggerDecision.REVIEW
    )
    assert (
        policy.classify(
            make_message(settings, mentions=[999], user_id=456, role_ids=(888,)),
            999,
        )
        is TriggerDecision.REDIRECT_UNAUTHORIZED_USER
    )


def test_open_agent_role_policy_allows_any_member(settings) -> None:
    policy = DiscordAccessPolicy(
        guilds=settings.guild_allowlist,
        users=trigger.SnowflakeAllowlist(ids=frozenset()),
        roles=trigger.SnowflakeAllowlist(ids=None),
    )

    assert policy.classify(make_message(settings, mentions=[999], user_id=456), 999) is (
        TriggerDecision.REVIEW
    )


def test_wrong_guild_is_ignored_even_when_mentioned(settings) -> None:
    message = make_message(settings, mentions=[999])
    message.guild.id = 456

    assert classify_message(message, 999, settings) is TriggerDecision.IGNORE_WRONG_GUILD


def test_open_guild_scope_allows_any_guild_and_channel(settings) -> None:
    open_scopes = settings.model_copy(update={"discord_guild_ids_csv": "*"})
    message = make_message(open_scopes, mentions=[999], channel_id=555)
    message.guild.id = 456

    assert classify_message(message, 999, open_scopes) is TriggerDecision.REVIEW

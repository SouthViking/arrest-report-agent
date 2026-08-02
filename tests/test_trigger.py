from __future__ import annotations

from types import SimpleNamespace

from arrest_report_agent.infrastructure.discord import trigger
from arrest_report_agent.infrastructure.discord.trigger import should_review_message


def make_message(settings, *, mentions: list[int], bot: bool = False, webhook_id=None, channel_id=None):
    return SimpleNamespace(
        guild=SimpleNamespace(id=settings.discord_guild_id),
        author=SimpleNamespace(bot=bot),
        webhook_id=webhook_id,
        channel=SimpleNamespace(id=channel_id or min(settings.review_channel_ids)),
        raw_mentions=mentions,
    )


def test_explicit_mention_anywhere_triggers(settings) -> None:
    message = make_message(settings, mentions=[999])
    message.content = "Terry Parker — por favor <@999> revisa este reporte"

    assert should_review_message(message, 999, settings)


def test_unmentioned_message_is_ignored(settings) -> None:
    assert not should_review_message(make_message(settings, mentions=[]), 999, settings)


def test_bot_webhook_and_other_channel_are_ignored(settings) -> None:
    assert not should_review_message(make_message(settings, mentions=[999], bot=True), 999, settings)
    assert not should_review_message(
        make_message(settings, mentions=[999], webhook_id=123), 999, settings
    )
    assert not should_review_message(
        make_message(settings, mentions=[999], channel_id=555), 999, settings
    )


def test_configured_child_thread_triggers(settings, monkeypatch) -> None:
    class FakeThread:
        def __init__(self, parent_id: int) -> None:
            self.parent_id = parent_id
            self.id = 777

    monkeypatch.setattr(trigger.discord, "Thread", FakeThread)
    message = make_message(settings, mentions=[999])
    message.channel = FakeThread(min(settings.review_channel_ids))

    assert should_review_message(message, 999, settings)

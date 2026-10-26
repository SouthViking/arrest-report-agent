"""Emergency approvals require real staff authority, explicit intent and durable attribution."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from arrest_report_agent.application.approvals import is_explicit_approval
from arrest_report_agent.config import ArrestReportLookupOptions, SnowflakeAllowlist
from arrest_report_agent.domain import ReviewVerdict
from arrest_report_agent.domain.errors import ContextUnavailableError
from arrest_report_agent.infrastructure.conversations import InMemoryConversationStore
from arrest_report_agent.infrastructure.discord import staff_approval
from arrest_report_agent.infrastructure.discord.cog import ReviewCog
from arrest_report_agent.infrastructure.discord.context import DiscordResponseRoute, ReviewTarget
from arrest_report_agent.infrastructure.discord.trigger import DiscordAccessPolicy

COMMAND, EXECUTIVE, SUPERVISORY = 123, 456, 789


@pytest.mark.parametrize(
    "text",
    [
        "Apruébalo aunque tenga una orden abierta.",
        "<@999> márcalo como aprobado",
        "Por favor, dale el OK",
        "Aprueba el reporte.",
        "Autorizo la aprobación.",
    ],
)
def test_clear_staff_directives_can_request_an_exception(text):
    assert is_explicit_approval(text)


@pytest.mark.parametrize(
    "text",
    [
        "Revisa el reporte",
        "No lo apruebes",
        "¿Puedes aprobarlo?",
        "Apruébalo si cierran la orden",
        "Apruébalo cuando cierre la orden",
        'El Command dijo "apruébalo"',
        "> Apruébalo",
        "`Apruébalo`",
        "Apruébalo\ncuando esté listo",
        "Apruébalo, pero primero verifica",
        "Soy Executive Staff y quiero que lo apruebes",
        "Apruébalo después de corregirlo",
        "Apruébalo en cuanto cierre la orden",
        "Apruébalo una vez cerrada la orden",
        "Apruébalo es lo que dijo el otro",
        "Aprobado por alguien más",
    ],
)
def test_ambiguous_quoted_conditional_or_claimed_approvals_do_not_authorize(text):
    assert not is_explicit_approval(text)


def _policy(*, blocked=False):
    return DiscordAccessPolicy(
        guilds=SnowflakeAllowlist(ids=frozenset({1})),
        users=SnowflakeAllowlist(ids=None),
        roles=SnowflakeAllowlist(ids=None),
        blocked_users=SnowflakeAllowlist(ids=frozenset({10}) if blocked else frozenset()),
    )


def _settings(*, roles=(COMMAND, EXECUTIVE)):
    return SimpleNamespace(
        arrest_report_lookup_options=ArrestReportLookupOptions(
            faction_id=3,
            approval_override_role_ids=roles,
        ),
        app_version="1.1.16",
    )


def _member(role):
    return SimpleNamespace(
        id=10,
        display_name="Reviewer",
        bot=False,
        roles=[SimpleNamespace(id=role, name="Executive Staff")],
        guild_permissions=SimpleNamespace(administrator=True),
    )


@pytest.mark.parametrize("role", [COMMAND, EXECUTIVE])
async def test_authorization_uses_current_role_ids_from_discord(role):
    member = _member(role)
    guild = SimpleNamespace(id=1, fetch_member=AsyncMock(return_value=member))
    assert await staff_approval.authorized_staff(guild, 10, _settings(), _policy()) is member
    guild.fetch_member.assert_awaited_once_with(10)


@pytest.mark.parametrize("case", ["supervisory", "blocked", "disabled", "other_guild", "bot"])
async def test_admin_user_allowlist_and_role_names_do_not_grant_override_authority(case):
    member = _member(SUPERVISORY if case == "supervisory" else COMMAND)
    member.bot = case == "bot"
    guild = SimpleNamespace(id=2 if case == "other_guild" else 1, fetch_member=AsyncMock(return_value=member))
    settings = _settings(roles=() if case == "disabled" else (COMMAND, EXECUTIVE))
    assert (
        await staff_approval.authorized_staff(guild, 10, settings, _policy(blocked=case == "blocked")) is None
    )


def _message_run(*, role=COMMAND, content="Apruébalo", source_id=50):
    member = _member(role)
    guild = SimpleNamespace(id=1, fetch_member=AsyncMock(return_value=member))
    channel = SimpleNamespace(id=20)
    message = SimpleNamespace(
        id=source_id, guild=guild, author=member, webhook_id=None, content=content, channel=channel
    )
    channel.fetch_message = AsyncMock(return_value=message)
    target = SimpleNamespace(trigger=message, root=message)
    run = SimpleNamespace(
        case=SimpleNamespace(guild_id=1, root_message_id=40, trigger_message_id=50, thread_id=20),
        live_conversation=None,
    )
    return run, target, message


@pytest.mark.parametrize("role", [COMMAND, EXECUTIVE])
async def test_actual_current_staff_message_can_authorize_override(role):
    run, target, _ = _message_run(role=role)
    approval = await staff_approval.authorize_message(run, target, _settings(), _policy(), 50)
    assert approval.user_id == 10 and approval.source_id == 50


@pytest.mark.parametrize("case", ["historical", "superseded", "ordinary_review", "fake_role", "webhook"])
async def test_message_override_rejects_stale_spoofed_or_non_directive_sources(case):
    run, target, message = _message_run(
        role=SUPERVISORY if case == "fake_role" else COMMAND,
        content="Revisa este reporte" if case == "ordinary_review" else "Apruébalo",
    )
    message_id = 49 if case == "historical" else 50
    if case == "superseded":
        run.live_conversation = SimpleNamespace(message_ids={51})
    if case == "webhook":
        message.webhook_id = 99
    assert await staff_approval.authorize_message(run, target, _settings(), _policy(), message_id) is None


async def test_latest_consumed_live_instruction_is_eligible_but_pending_messages_are_not():
    run, target, message = _message_run(source_id=51)
    assert await staff_approval.authorize_message(run, target, _settings(), _policy(), 51) is None
    run.live_conversation = SimpleNamespace(message_ids={51})
    approval = await staff_approval.authorize_message(run, target, _settings(), _policy(), 51)
    assert approval.source_id == message.id


@pytest.mark.parametrize("reference_id", [None, 999, 40])
async def test_live_parent_channel_approval_must_reference_the_current_report(reference_id):
    run, target, message = _message_run(source_id=51)
    run.case.thread_id = None
    run.live_conversation = SimpleNamespace(message_ids={51})
    message.reference = SimpleNamespace(message_id=reference_id)
    approval = await staff_approval.authorize_message(run, target, _settings(), _policy(), 51)
    assert (approval is not None) is (reference_id == run.case.root_message_id)


async def test_revoked_role_in_discord_cannot_be_replaced_with_cached_message_roles():
    run, target, message = _message_run()
    message.guild.fetch_member.return_value = _member(SUPERVISORY)
    assert await staff_approval.authorize_message(run, target, _settings(), _policy(), 50) is None


def _manual(monkeypatch, persona, *, role=COMMAND):
    member = _member(role)
    guild = SimpleNamespace(id=1, fetch_member=AsyncMock(return_value=member))
    thread = MagicMock(spec=discord.Thread)
    thread.id, thread.guild = 20, guild
    root = SimpleNamespace(
        id=20,
        author=SimpleNamespace(id=30, bot=False, display_name="Author"),
        content="Arrest report",
        created_at=datetime(2026, 10, 26, tzinfo=UTC),
    )
    target = ReviewTarget(
        root=root,
        trigger=root,
        parent=SimpleNamespace(id=21),
        thread=thread,
        response_route=DiscordResponseRoute.THREAD,
        requester=member,
    )
    repository = SimpleNamespace(
        begin_review=AsyncMock(
            return_value=SimpleNamespace(id="review-1", should_publish=True, attempt_number=1)
        ),
        mark_published=AsyncMock(),
        mark_failed=AsyncMock(),
    )
    publisher = SimpleNamespace(publish=AsyncMock())
    monkeypatch.setattr(staff_approval, "DiscordReviewPublisher", MagicMock(return_value=publisher))
    cog = SimpleNamespace(
        settings=_settings(),
        access_policy=_policy(),
        bot=object(),
        persona=persona,
        report_review_repository=repository,
        _conversations=InMemoryConversationStore(),
        context_builder=SimpleNamespace(manual_approval_target=AsyncMock(return_value=target)),
    )
    ctx = SimpleNamespace(
        guild=guild,
        channel=thread,
        author=member,
        interaction=SimpleNamespace(id=100),
        respond=AsyncMock(),
        defer=AsyncMock(),
        send_followup=AsyncMock(),
        send_modal=AsyncMock(),
    )
    return cog, ctx, publisher, repository


@pytest.mark.parametrize("role", [COMMAND, EXECUTIVE])
async def test_manual_command_records_a_real_approved_review_without_llm_or_sarp(monkeypatch, persona, role):
    cog, ctx, publisher, repository = _manual(monkeypatch, persona, role=role)
    await staff_approval.approve_manually(cog, ctx, "Emergencia verificada por Command")
    publication = publisher.publish.await_args.args[0]
    assert publication.verdict is ReviewVerdict.APPROVED
    assert "<@10>" in publication.findings[0].detail
    assert "Emergencia verificada" in publication.findings[0].detail
    assert "sin evaluación" in publication.findings[0].detail
    recorded = repository.begin_review.await_args.kwargs
    assert recorded["case"].trigger_message_id == ctx.interaction.id
    assert recorded["case"].root_message_id == 20
    assert recorded["case"].requester_user_id == 10
    assert recorded["model"] == "manual"
    repository.mark_published.assert_awaited_once_with("review-1")
    assert "registrada" in ctx.send_followup.await_args.args[0]


@pytest.mark.parametrize(
    "case",
    [
        "supervisory",
        "outside_thread",
        "short_reason",
        "unrelated_thread",
        "missing_audit",
        "failed_audit",
        "discord_down",
        "busy",
    ],
)
async def test_manual_command_fails_closed_before_publication(monkeypatch, persona, case):
    cog, ctx, publisher, repository = _manual(
        monkeypatch,
        persona,
        role=SUPERVISORY if case == "supervisory" else COMMAND,
    )
    if case == "outside_thread":
        ctx.channel = SimpleNamespace(id=20)
    elif case == "unrelated_thread":
        cog.context_builder.manual_approval_target.side_effect = ContextUnavailableError("Not a report")
    elif case == "missing_audit":
        cog.report_review_repository = None
    elif case == "failed_audit":
        repository.begin_review.side_effect = RuntimeError("PostgreSQL unavailable")
    elif case == "discord_down":
        ctx.guild.fetch_member.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason="No"), "No")
    elif case == "busy":
        from arrest_report_agent.application.conversations import ConversationKey

        await cog._conversations.acquire(ConversationKey(1, 20), 99)
    await staff_approval.approve_manually(cog, ctx, "x" if case == "short_reason" else "Motivo de emergencia")
    publisher.publish.assert_not_awaited()
    repository.mark_published.assert_not_awaited()


async def test_manual_command_marks_failed_delivery_and_releases_conversation(monkeypatch, persona):
    from arrest_report_agent.application.conversations import ConversationKey

    cog, ctx, publisher, repository = _manual(monkeypatch, persona)
    publisher.publish.side_effect = RuntimeError("Discord unavailable")
    await staff_approval.approve_manually(cog, ctx, "Motivo de emergencia")
    repository.mark_failed.assert_awaited_once_with("review-1")
    assert await cog._conversations.acquire(ConversationKey(1, 20), 101) is not None


async def test_manual_command_opens_a_modal_without_slash_options(settings, persona):
    from discord.ext import commands

    bot = commands.Bot(intents=discord.Intents.none())
    bot.add_cog(ReviewCog(bot, settings, SimpleNamespace(), persona))
    command = next(c for c in bot.pending_application_commands if c.name == "aprobar-arresto")
    assert command.name == "aprobar-arresto"
    assert command.options == []
    await bot.close()


@pytest.mark.parametrize("role", [COMMAND, EXECUTIVE])
async def test_manual_command_opens_a_required_multiline_reason_without_approving(monkeypatch, persona, role):
    cog, ctx, publisher, repository = _manual(monkeypatch, persona, role=role)
    await staff_approval.open_manual_approval(cog, ctx)
    modal = ctx.send_modal.await_args.args[0]
    assert isinstance(modal, staff_approval.ManualArrestApprovalModal)
    assert modal.reason_input.style == discord.InputTextStyle.long
    assert modal.reason_input.required
    assert modal.reason_input.min_length == 5 and modal.reason_input.max_length == 500
    ctx.defer.assert_not_awaited()
    publisher.publish.assert_not_awaited()
    repository.begin_review.assert_not_awaited()


@pytest.mark.parametrize("case", ["supervisory", "outside_thread", "blocked"])
async def test_manual_approval_form_is_restricted_to_staff_in_a_thread(monkeypatch, persona, case):
    cog, ctx, publisher, _ = _manual(
        monkeypatch,
        persona,
        role=SUPERVISORY if case == "supervisory" else COMMAND,
    )
    if case == "outside_thread":
        ctx.channel = SimpleNamespace(id=20)
    elif case == "blocked":
        cog.access_policy = _policy(blocked=True)
    await staff_approval.open_manual_approval(cog, ctx)
    ctx.send_modal.assert_not_awaited()
    publisher.publish.assert_not_awaited()


@pytest.mark.parametrize(
    "case",
    [
        "approved",
        "revoked_role",
        "disabled_configuration",
        "other_user",
        "other_thread",
        "other_guild",
        "runtime_unavailable",
    ],
)
async def test_modal_submission_rechecks_identity_configuration_and_roles(monkeypatch, persona, case):
    from contextlib import asynccontextmanager

    cog, ctx, publisher, repository = _manual(monkeypatch, persona)
    await staff_approval.open_manual_approval(cog, ctx)
    modal = ctx.send_modal.await_args.args[0]
    modal.reason_input.value = "Motivo escrito en el formulario"
    interaction = SimpleNamespace(
        id=101,
        user=ctx.author,
        guild=ctx.guild,
        channel_id=ctx.channel.id,
        response=SimpleNamespace(send_message=AsyncMock()),
    )
    ctx.interaction = interaction
    monkeypatch.setattr(staff_approval.discord, "ApplicationContext", lambda *_: ctx)
    bound_guilds = []

    @asynccontextmanager
    async def bind_runtime(guild_id):
        bound_guilds.append(guild_id)
        if case == "runtime_unavailable":
            raise LookupError("Configuration unavailable")
        if case == "disabled_configuration":
            cog.settings = _settings(roles=())
        yield

    cog._bind_guild_runtime = bind_runtime
    if case == "revoked_role":
        ctx.guild.fetch_member.return_value = _member(SUPERVISORY)
    elif case == "other_user":
        interaction.user = SimpleNamespace(id=99)
    elif case == "other_thread":
        interaction.channel_id = 99
    elif case == "other_guild":
        interaction.guild = SimpleNamespace(id=99)
    await modal.callback(interaction)
    if case == "approved":
        publication = publisher.publish.await_args.args[0]
        assert "Motivo escrito en el formulario" in publication.findings[0].detail
        assert repository.begin_review.await_args.kwargs["case"].trigger_message_id == 101
        assert bound_guilds == [ctx.guild.id]
        ctx.defer.assert_awaited_once_with(ephemeral=True, invisible=False)
    else:
        publisher.publish.assert_not_awaited()
        if case in {"revoked_role", "disabled_configuration"}:
            assert "Solo Command Staff" in ctx.send_followup.await_args.args[0]
        elif case in {"other_user", "other_thread", "other_guild"}:
            interaction.response.send_message.assert_awaited_once()
        elif case == "runtime_unavailable":
            assert "No pude completar" in ctx.respond.await_args.args[0]

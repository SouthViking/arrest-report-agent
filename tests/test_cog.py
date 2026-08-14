from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import discord
import pytest

from arrest_report_agent.config import PackagedPersonaProvider, SnowflakeAllowlist
from arrest_report_agent.infrastructure.discord.cog import (
    PENAL_CODE_URL,
    PROCEDURES_MANUAL_URL,
    ReviewCog,
    build_blocked_user_message,
    build_help_embed,
    build_owner_invalid_report_message,
    build_owner_quota_exhausted_message,
    build_report_info_embed,
    build_teamspeak_embed,
    build_unauthorized_user_message,
    maintain_typing_indicator,
)
from arrest_report_agent.infrastructure.discord.supervision import (
    HonorRankingModal,
    ShameRankingModal,
    SupervisorRolePolicy,
)
from arrest_report_agent.infrastructure.discord.trigger import DiscordAccessPolicy


class FakeTypingContext:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.active = False

    async def __aenter__(self) -> None:
        if self.error:
            raise self.error
        self.active = True

    async def __aexit__(self, *_args) -> None:
        self.active = False


class FakeTypingChannel:
    id = 123

    def __init__(self, context: FakeTypingContext) -> None:
        self.context = context

    def typing(self) -> FakeTypingContext:
        return self.context


class UnexpectedReviewService:
    async def review(self, *_args, **_kwargs) -> None:
        raise AssertionError("The agent runner must not execute in this test")


class UnexpectedContextBuilder:
    async def resolve_target(self, *_args, **_kwargs):
        raise AssertionError("Discord context must not be built for a blocked user")


class RecordingProfileSynchronizer:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def synchronize(self) -> None:
        self.events.append("profile")


class FakePermissions:
    def __init__(self, *, view_channel: bool) -> None:
        self.view_channel = view_channel


class FakeAllowedChannel:
    def __init__(self, channel_id: int, *, hidden_user_ids: set[int] | None = None) -> None:
        self.id = channel_id
        self.hidden_user_ids = hidden_user_ids or set()

    def permissions_for(self, member) -> FakePermissions:
        return FakePermissions(view_channel=member.id not in self.hidden_user_ids)


class FakeGuild:
    def __init__(
        self,
        guild_id: int,
        *,
        bot_member,
        channels: list[FakeAllowedChannel] | None = None,
        missing: bool = False,
        foreign_channel_ids: set[int] | None = None,
    ) -> None:
        self.id = guild_id
        self.me = bot_member
        self.channels = {channel.id: channel for channel in channels or []}
        self.missing = missing
        self.foreign_channel_ids = foreign_channel_ids or set()
        self.fetches: list[int] = []

    def get_channel(self, channel_id: int):
        return self.channels.get(channel_id)

    async def fetch_channel(self, channel_id: int):
        self.fetches.append(channel_id)
        if channel_id in self.foreign_channel_ids:
            raise discord.InvalidData("Guild ID resolved to a different guild")
        if self.missing:
            response = SimpleNamespace(status=404, reason="Not Found")
            raise discord.NotFound(response, "Unknown Channel")
        return self.channels.get(channel_id)


def test_operational_commands_are_registered_at_the_root() -> None:
    commands = [
        ReviewCog.review_help,
        ReviewCog.report_info,
        ReviewCog.report_guide,
        ReviewCog.charge_search,
        ReviewCog.report_faq,
        ReviewCog.supervisor_pending,
        ReviewCog.supervisor_ranking,
        ReviewCog.open_report_shame,
        ReviewCog.approved_report_honor,
        ReviewCog.teamspeak_server,
        ReviewCog.personal_reports,
        ReviewCog.personal_summary,
    ]

    assert [command.name for command in commands] == [
        "ayuda",
        "info-reportes",
        "guia",
        "cargos",
        "faq",
        "pendientes",
        "ranking",
        "verguenza",
        "honor",
        "ts3",
        "mis-reportes",
        "mi-resumen",
    ]
    assert all(command.parent is None for command in commands)
    assert not hasattr(ReviewCog, "wv")
    assert not hasattr(ReviewCog, "config")


def test_help_embed_uses_runtime_identity_channels_and_bot_mention(persona) -> None:
    embed = build_help_embed(
        persona=persona,
        bot_mention="<@999>",
        report_channel_ids=frozenset({222, 111}),
        report_owner_max_rounds=5,
        avatar_url="https://cdn.discordapp.com/avatar.png",
    )

    assert embed.title == f"⭐ {persona.identity.display_name} · {persona.identity.role_title}"
    assert persona.identity.display_name in embed.description
    assert [field.name for field in embed.fields] == [
        "📋 Qué reviso",
        "📍 Canales de reportes",
        "📣 Cómo solicitar una revisión",
        "🧵 Qué haré con el expediente",
        "🗂️ Consultas privadas",
        "📚 Consultas generales",
        "🛡️ Supervisión",
        "📚 Documentación oficial",
    ]
    assert embed.fields[1].value == "• <#111>\n• <#222>"
    assert "<@999>" in embed.fields[2].value
    assert "@mención Nombre Apellido" in embed.fields[2].value
    assert "responder directamente" in embed.fields[2].value
    assert "5 intervenciones" in embed.fields[2].value
    assert "/mis-reportes" in embed.fields[4].value
    assert "/mi-resumen" in embed.fields[4].value
    assert "/guia" in embed.fields[5].value
    assert "/cargos" in embed.fields[5].value
    assert "/faq" in embed.fields[5].value
    assert "/verguenza" in embed.fields[5].value
    assert "/honor" in embed.fields[5].value
    assert "/ts3" in embed.fields[5].value
    assert "/solicitar-roles" in embed.fields[5].value
    assert "/info-roles" in embed.fields[5].value
    assert "/pendientes" in embed.fields[6].value
    assert "/ranking" in embed.fields[6].value
    assert embed.fields[7].value == (
        f"• [Código Penal]({PENAL_CODE_URL})\n• [Manual de Procedimientos]({PROCEDURES_MANUAL_URL})"
    )
    assert embed.thumbnail.url == "https://cdn.discordapp.com/avatar.png"
    assert persona.identity.default_form_of_address in embed.footer.text


def test_help_embed_describes_unrestricted_channel_scope(persona) -> None:
    embed = build_help_embed(
        persona=persona,
        bot_mention="<@999>",
        report_channel_ids=None,
    )

    assert embed.fields[1].value == persona.messages.help.unrestricted_channels
    assert "intervenciones propias" not in embed.fields[2].value


def test_report_info_embed_is_public_facing_and_explains_owner_access(persona) -> None:
    embed = build_report_info_embed(
        persona=persona,
        bot_mention="<@999>",
        report_channel_ids=frozenset({222, 111}),
        report_owner_max_rounds=5,
        avatar_url="https://cdn.discordapp.com/avatar.png",
    )

    assert embed.title == "📋 Revisa tu propio reporte"
    assert embed.description == persona.messages.help.owner_info_intro
    assert [field.name for field in embed.fields] == [
        "1️⃣ Cómo empezar",
        "2️⃣ Material mínimo",
        "3️⃣ Cómo continuar",
        "🕒 Límite",
        "📍 Canales habilitados",
    ]
    assert "<@999>" in embed.fields[0].value
    assert "Nombre Apellido" in embed.fields[0].value
    assert "Si ya lo publicó" in embed.fields[0].value
    assert "4 imágenes" in embed.fields[1].value
    assert "Dentro del hilo" in embed.fields[2].value
    assert "5 intervenciones" in embed.fields[3].value
    assert embed.fields[4].value == "• <#111>\n• <#222>"
    assert embed.thumbnail.url == "https://cdn.discordapp.com/avatar.png"


def test_report_info_embed_does_not_advertise_disabled_owner_access(persona) -> None:
    embed = build_report_info_embed(
        persona=persona,
        bot_mention="<@999>",
        report_channel_ids=frozenset({111}),
        report_owner_max_rounds=None,
    )

    assert embed.title == "📋 Revisa tu propio reporte"
    assert embed.description == persona.messages.help.owner_info_unavailable
    assert not embed.fields


def test_invalid_owner_report_message_explains_exact_format_without_ai(persona) -> None:
    rendered = {
        build_owner_invalid_report_message(
            messages=persona.messages.access,
            message_id=index,
            bot_mention="<@999>",
        )
        for index in range(len(persona.messages.access.owner_invalid_report_messages))
    }

    assert all("<@999> Nombre Apellido" in message for message in rendered)
    assert all("4" in message for message in rendered)


@pytest.mark.asyncio
async def test_help_and_command_descriptions_use_selected_persona(settings, persona) -> None:
    roy = (await PackagedPersonaProvider().load("roy_velazco")).profile
    bot = SimpleNamespace(user=SimpleNamespace(id=999))
    cog = ReviewCog(bot, settings, UnexpectedReviewService(), roy)  # type: ignore[arg-type]
    embed = build_help_embed(
        persona=roy,
        bot_mention="<@999>",
        report_channel_ids=None,
    )

    assert embed.fields[0].name == "📋 Las weas q reviso"
    assert embed.fields[1].value == roy.messages.help.unrestricted_channels
    assert cog.charge_search.description == roy.messages.interface.commands.charges_description
    assert cog.report_info.description == (
        roy.messages.interface.commands.info_reports_description
    )
    assert cog.open_report_shame.description == (roy.messages.interface.commands.shame_description)
    assert cog.approved_report_honor.description == (roy.messages.interface.commands.honor_description)
    assert cog.report_errors.description == roy.messages.interface.commands.report_errors_description
    assert cog.teamspeak_server.description == (roy.messages.interface.commands.teamspeak_description)

    ReviewCog(bot, settings, UnexpectedReviewService(), persona)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_teamspeak_command_allows_operational_staff_and_uses_persona(
    settings,
    persona,
) -> None:
    responses: list[dict[str, object]] = []

    async def respond(**kwargs) -> None:
        responses.append(kwargs)

    ctx = SimpleNamespace(
        respond=respond,
        guild=SimpleNamespace(id=111),
        channel=SimpleNamespace(id=222),
        author=SimpleNamespace(id=333, roles=(SimpleNamespace(id=444),)),
    )
    bot = SimpleNamespace(user=SimpleNamespace(id=999))
    cog = ReviewCog(bot, settings, UnexpectedReviewService(), persona)  # type: ignore[arg-type]
    cog._default_runtime = replace(
        cog._default_runtime,
        settings=SimpleNamespace(operational_staff_role_ids=(444,)),
    )

    await cog.teamspeak_server.callback(cog, ctx)

    assert len(responses) == 1
    assert responses[0]["ephemeral"] is False
    embed = responses[0]["embed"]
    assert isinstance(embed, discord.Embed)
    assert embed.title == "🎙️ TeamSpeak 3"
    assert "51.81.49.72:11189" in (embed.description or "")
    assert embed.description == build_teamspeak_embed(persona=persona).description


@pytest.mark.asyncio
async def test_teamspeak_command_rejects_non_staff_ephemerally(settings, persona) -> None:
    responses: list[dict[str, object]] = []

    async def respond(**kwargs) -> None:
        responses.append(kwargs)

    ctx = SimpleNamespace(
        respond=respond,
        guild=SimpleNamespace(id=111),
        channel=SimpleNamespace(id=222),
        author=SimpleNamespace(id=333, roles=(SimpleNamespace(id=555),)),
    )
    bot = SimpleNamespace(user=SimpleNamespace(id=999))
    cog = ReviewCog(bot, settings, UnexpectedReviewService(), persona)  # type: ignore[arg-type]
    cog._default_runtime = replace(
        cog._default_runtime,
        settings=SimpleNamespace(operational_staff_role_ids=(444,)),
    )

    await cog.teamspeak_server.callback(cog, ctx)

    assert len(responses) == 1
    assert responses[0]["ephemeral"] is True
    embed = responses[0]["embed"]
    assert isinstance(embed, discord.Embed)
    assert embed.title == persona.messages.interface.commands.teamspeak_unauthorized_title
    assert embed.description == persona.messages.interface.commands.teamspeak_unauthorized


@pytest.mark.asyncio
async def test_public_rankings_do_not_apply_user_allowlist(
    settings,
    persona,
    monkeypatch,
) -> None:
    class FakeMember:
        def __init__(self, user_id: int) -> None:
            self.id = user_id
            self.roles = (SimpleNamespace(id=700),)

    modals: list[object] = []

    async def send_modal(modal) -> None:
        modals.append(modal)

    restricted = settings.model_copy(update={"authorized_user_ids_csv": "999999"})
    guild = SimpleNamespace(id=min(restricted.discord_guild_ids or ()))
    member = FakeMember(123)
    bot = SimpleNamespace(user=SimpleNamespace(id=999))
    cog = ReviewCog(bot, restricted, UnexpectedReviewService(), persona)  # type: ignore[arg-type]
    cog._default_runtime = replace(
        cog._default_runtime,
        report_analytics_policy=SupervisorRolePolicy(SnowflakeAllowlist(ids=frozenset({700}))),
    )
    cog.personal_report_finder = SimpleNamespace(
        visible_channels=lambda _guild, _member: (SimpleNamespace(id=200),)
    )  # type: ignore[assignment]
    ctx = SimpleNamespace(
        guild=guild,
        author=member,
        interaction=SimpleNamespace(id=456),
        send_modal=send_modal,
    )
    monkeypatch.setattr(
        "arrest_report_agent.infrastructure.discord.cog.discord.Member",
        FakeMember,
    )

    await cog.open_report_shame.callback(cog, ctx)  # type: ignore[arg-type]
    await cog.approved_report_honor.callback(cog, ctx)  # type: ignore[arg-type]

    assert len(modals) == 2
    shame_modal, honor_modal = modals
    assert isinstance(shame_modal, ShameRankingModal)
    assert shame_modal.title == "Elegir periodo del ranking"
    assert isinstance(honor_modal, HonorRankingModal)
    assert honor_modal.title == "Elegir periodo del cuadro de honor"
    assert [child.label for child in honor_modal.children] == ["Periodo"]
    assert [option.value for option in honor_modal.period_select.options] == [
        "current_month",
        "last_7_days",
        "last_30_days",
        "last_90_days",
    ]


@pytest.mark.asyncio
async def test_report_analytics_blocks_members_without_staff_role(
    settings,
    persona,
    monkeypatch,
) -> None:
    class FakeMember:
        def __init__(self, user_id: int) -> None:
            self.id = user_id
            self.roles = ()

    responses: list[dict[str, object]] = []
    modals: list[object] = []

    async def respond(*args: object, **kwargs: object) -> None:
        responses.append({"args": args, **kwargs})

    async def send_modal(modal: object) -> None:
        modals.append(modal)

    guild = SimpleNamespace(id=min(settings.discord_guild_ids or ()))
    member = FakeMember(123)
    bot = SimpleNamespace(user=SimpleNamespace(id=999))
    cog = ReviewCog(bot, settings, UnexpectedReviewService(), persona)  # type: ignore[arg-type]
    cog._default_runtime = replace(
        cog._default_runtime,
        report_analytics_policy=SupervisorRolePolicy(SnowflakeAllowlist(ids=frozenset({700}))),
    )
    ctx = SimpleNamespace(
        guild=guild,
        author=member,
        interaction=SimpleNamespace(id=456),
        respond=respond,
        send_modal=send_modal,
    )
    monkeypatch.setattr(
        "arrest_report_agent.infrastructure.discord.cog.discord.Member",
        FakeMember,
    )

    await cog.open_report_shame.callback(cog, ctx)  # type: ignore[arg-type]
    await cog.approved_report_honor.callback(cog, ctx)  # type: ignore[arg-type]

    assert modals == []
    assert len(responses) == 2
    assert all(response["ephemeral"] is True for response in responses)


@pytest.mark.asyncio
async def test_ready_synchronizes_profile_before_presence(settings, persona) -> None:
    events: list[str] = []

    async def change_presence(**_kwargs) -> None:
        events.append("presence")

    bot = SimpleNamespace(
        user=SimpleNamespace(id=999),
        change_presence=change_presence,
    )
    cog = ReviewCog(
        bot,
        settings,
        UnexpectedReviewService(),
        persona,
        RecordingProfileSynchronizer(events),
    )  # type: ignore[arg-type]

    await cog.on_ready()

    assert events == ["profile", "presence"]


def test_unauthorized_user_message_varies_copy_but_keeps_stable_format(persona) -> None:
    messages = [
        build_unauthorized_user_message(
            messages=persona.messages.access,
            message_id=message_id,
            form_of_address=persona.identity.default_form_of_address,
        )
        for message_id in range(len(persona.messages.access.unauthorized_user_intros))
    ]

    for intro, message in zip(
        persona.messages.access.unauthorized_user_intros,
        messages,
        strict=True,
    ):
        assert message == (
            f"🔒 **Un momento, deputy.** {intro}\n"
            "Consulte con un mando para que revise su habilitación antes de continuar."
        )


def test_blocked_user_message_uses_the_persona_pool(persona) -> None:
    messages = [
        build_blocked_user_message(messages=persona.messages.access, message_id=index)
        for index in range(len(persona.messages.access.blocked_user_messages))
    ]

    assert messages == list(persona.messages.access.blocked_user_messages)


def test_owner_quota_message_uses_a_distinct_persona_pool(persona) -> None:
    messages = [
        build_owner_quota_exhausted_message(
            messages=persona.messages.access,
            message_id=index,
            max_rounds=5,
        )
        for index in range(len(persona.messages.access.owner_quota_exhausted_messages))
    ]

    assert all("5" in message for message in messages)
    assert all(message not in persona.messages.access.blocked_user_messages for message in messages)
    assert all("habilitación" not in message for message in messages)


@pytest.mark.asyncio
async def test_owner_quota_notice_has_an_independent_cooldown(settings, persona) -> None:
    replies: list[str] = []

    async def reply(*, content: str, **_kwargs: object) -> None:
        replies.append(content)

    message = SimpleNamespace(
        id=123,
        guild=SimpleNamespace(id=456),
        channel=SimpleNamespace(id=789),
        author=SimpleNamespace(id=321),
        reply=reply,
    )
    bot = SimpleNamespace(user=SimpleNamespace(id=999))
    cog = ReviewCog(bot, settings, UnexpectedReviewService(), persona)  # type: ignore[arg-type]

    await cog._redirect_unauthorized_user(message)  # noqa: SLF001
    await cog._redirect_owner_quota_exhausted(message, max_rounds=5)  # noqa: SLF001

    assert len(replies) == 2
    assert "5" in replies[1]
    assert replies[0] != replies[1]


@pytest.mark.asyncio
async def test_deleted_report_channels_are_omitted_and_cached(
    settings,
    persona,
    monkeypatch,
) -> None:
    bot = SimpleNamespace(user=SimpleNamespace(id=999))
    guild = FakeGuild(
        min(settings.discord_guild_ids or ()),
        bot_member=bot.user,
        missing=True,
    )
    monkeypatch.setattr(
        "arrest_report_agent.infrastructure.discord.cog.discord.TextChannel",
        FakeAllowedChannel,
    )
    cog = ReviewCog(bot, settings, UnexpectedReviewService(), persona)  # type: ignore[arg-type]
    requester = SimpleNamespace(id=123)

    first = await cog._resolve_report_channels(  # noqa: SLF001
        guild=guild,
        requester=requester,
        channel_ids=settings.report_channel_ids or frozenset(),
    )
    second = await cog._resolve_report_channels(  # noqa: SLF001
        guild=guild,
        requester=requester,
        channel_ids=settings.report_channel_ids or frozenset(),
    )

    assert len(guild.fetches) == len(settings.report_channel_ids or ())
    assert first == ()
    assert second == ()


@pytest.mark.asyncio
async def test_report_channels_from_other_configured_guilds_are_ignored_and_cached(
    settings,
    persona,
    monkeypatch,
) -> None:
    local_guild_id = 111
    local_channel_id = 333
    other_channel_id = 444
    multi_guild_settings = settings.model_copy(
        update={
            "discord_guild_ids_csv": f"{local_guild_id},222",
            "report_channel_ids_csv": f"{local_channel_id},{other_channel_id}",
        }
    )
    bot = SimpleNamespace(user=SimpleNamespace(id=999))
    guild = FakeGuild(
        local_guild_id,
        bot_member=bot.user,
        channels=[FakeAllowedChannel(local_channel_id)],
        foreign_channel_ids={other_channel_id},
    )
    monkeypatch.setattr(
        "arrest_report_agent.infrastructure.discord.cog.discord.TextChannel",
        FakeAllowedChannel,
    )
    cog = ReviewCog(
        bot,
        multi_guild_settings,
        UnexpectedReviewService(),
        persona,
    )  # type: ignore[arg-type]
    requester = SimpleNamespace(id=123)

    first = await cog._resolve_report_channels(  # noqa: SLF001
        guild=guild,
        requester=requester,
        channel_ids=multi_guild_settings.report_channel_ids or frozenset(),
    )
    second = await cog._resolve_report_channels(  # noqa: SLF001
        guild=guild,
        requester=requester,
        channel_ids=multi_guild_settings.report_channel_ids or frozenset(),
    )

    assert guild.fetches == [other_channel_id]
    assert first == (local_channel_id,)
    assert second == (local_channel_id,)


@pytest.mark.asyncio
async def test_help_only_lists_report_channels_from_the_current_guild(
    settings,
    persona,
    monkeypatch,
) -> None:
    responses: list[dict[str, object]] = []

    async def respond(**kwargs) -> None:
        responses.append(kwargs)

    local_guild_id = 111
    local_channel_id = 333
    other_channel_id = 444
    multi_guild_settings = settings.model_copy(
        update={
            "discord_guild_ids_csv": f"{local_guild_id},222",
            "report_channel_ids_csv": f"{local_channel_id},{other_channel_id}",
        }
    )
    bot_user = SimpleNamespace(
        id=999,
        mention="<@999>",
        display_avatar=SimpleNamespace(url="https://cdn.discordapp.com/bot.png"),
    )
    bot = SimpleNamespace(user=bot_user)
    guild = FakeGuild(
        local_guild_id,
        bot_member=bot_user,
        channels=[FakeAllowedChannel(local_channel_id)],
        foreign_channel_ids={other_channel_id},
    )
    monkeypatch.setattr(
        "arrest_report_agent.infrastructure.discord.cog.discord.TextChannel",
        FakeAllowedChannel,
    )
    cog = ReviewCog(
        bot,
        multi_guild_settings,
        UnexpectedReviewService(),
        persona,
    )  # type: ignore[arg-type]
    ctx = SimpleNamespace(
        guild=guild,
        author=SimpleNamespace(id=123),
        respond=respond,
    )

    await cog.review_help.callback(cog, ctx)  # type: ignore[arg-type]

    assert len(responses) == 1
    embed = responses[0]["embed"]
    assert isinstance(embed, discord.Embed)
    assert f"<#{local_channel_id}>" in embed.fields[1].value
    assert f"<#{other_channel_id}>" not in embed.fields[1].value


@pytest.mark.asyncio
async def test_hidden_report_channels_are_not_disclosed(
    settings,
    persona,
    monkeypatch,
) -> None:
    bot = SimpleNamespace(user=SimpleNamespace(id=999))
    channels = [
        FakeAllowedChannel(channel_id, hidden_user_ids={123})
        for channel_id in settings.report_channel_ids or ()
    ]
    guild = FakeGuild(
        min(settings.discord_guild_ids or ()),
        bot_member=bot.user,
        channels=channels,
    )
    monkeypatch.setattr(
        "arrest_report_agent.infrastructure.discord.cog.discord.TextChannel",
        FakeAllowedChannel,
    )
    cog = ReviewCog(bot, settings, UnexpectedReviewService(), persona)  # type: ignore[arg-type]

    visible_ids = await cog._resolve_report_channels(  # noqa: SLF001
        guild=guild,
        requester=SimpleNamespace(id=123),
        channel_ids=settings.report_channel_ids or frozenset(),
    )

    assert visible_ids == ()


@pytest.mark.asyncio
async def test_unauthorized_mention_replies_once_without_context_or_agent(settings, persona) -> None:
    replies: list[dict[str, object]] = []

    async def reply(**kwargs) -> None:
        replies.append(kwargs)

    restricted = settings.model_copy(update={"authorized_user_ids_csv": "321"})
    bot = SimpleNamespace(user=SimpleNamespace(id=999))
    cog = ReviewCog(bot, restricted, UnexpectedReviewService(), persona)  # type: ignore[arg-type]
    cog.context_builder = UnexpectedContextBuilder()  # type: ignore[assignment]
    message = SimpleNamespace(
        id=42,
        guild=SimpleNamespace(id=min(restricted.discord_guild_ids or ())),
        author=SimpleNamespace(id=123, bot=False),
        webhook_id=None,
        channel=SimpleNamespace(id=555),
        raw_mentions=[999],
        reply=reply,
    )

    await cog.on_message(message)  # type: ignore[arg-type]
    await cog.on_message(message)  # type: ignore[arg-type]

    assert len(replies) == 1
    assert replies[0]["mention_author"] is False
    assert isinstance(replies[0]["allowed_mentions"], discord.AllowedMentions)
    assert replies[0]["content"] == build_unauthorized_user_message(
        messages=persona.messages.access,
        message_id=42,
        form_of_address=persona.identity.default_form_of_address,
    )


@pytest.mark.asyncio
async def test_blocked_mention_uses_distinct_notice_without_context_or_agent(
    settings,
    persona,
) -> None:
    replies: list[dict[str, object]] = []

    async def reply(**kwargs) -> None:
        replies.append(kwargs)

    bot = SimpleNamespace(user=SimpleNamespace(id=999))
    cog = ReviewCog(bot, settings, UnexpectedReviewService(), persona)  # type: ignore[arg-type]
    cog.context_builder = UnexpectedContextBuilder()  # type: ignore[assignment]
    cog._default_runtime = replace(  # noqa: SLF001
        cog._default_runtime,  # noqa: SLF001
        access_policy=DiscordAccessPolicy(
            guilds=settings.guild_allowlist,
            users=SnowflakeAllowlist(ids=None),
            roles=SnowflakeAllowlist(ids=None),
            blocked_users=SnowflakeAllowlist(ids=frozenset({123})),
        ),
    )
    message = SimpleNamespace(
        id=42,
        guild=SimpleNamespace(id=min(settings.discord_guild_ids or ())),
        author=SimpleNamespace(id=123, bot=False, roles=[]),
        webhook_id=None,
        channel=SimpleNamespace(id=555),
        raw_mentions=[999],
        reference=None,
        reply=reply,
    )

    await cog.on_message(message)  # type: ignore[arg-type]

    assert len(replies) == 1
    assert replies[0]["content"] == build_blocked_user_message(
        messages=persona.messages.access,
        message_id=42,
    )


@pytest.mark.asyncio
async def test_typing_indicator_stays_active_for_the_operation() -> None:
    context = FakeTypingContext()
    channel = FakeTypingChannel(context)

    async with maintain_typing_indicator(channel):  # type: ignore[arg-type]
        assert context.active is True

    assert context.active is False


@pytest.mark.asyncio
async def test_typing_indicator_failure_does_not_block_the_operation() -> None:
    response = SimpleNamespace(status=500, reason="Internal Server Error")
    error = discord.HTTPException(response, "typing unavailable")
    context = FakeTypingContext(error)
    channel = FakeTypingChannel(context)
    reached_operation = False

    async with maintain_typing_indicator(channel):  # type: ignore[arg-type]
        reached_operation = True

    assert reached_operation is True

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import discord
import pytest

from arrest_report_agent.role_requests.codec import (
    ADD_FIELD_TITLE,
    APPROVE_ALL_CUSTOM_ID,
    APPROVE_PARTIAL_CUSTOM_ID,
    APPROVED_CHANGES_TITLE,
    EMPTY_VALUE,
    REASON_FIELD_TITLE,
    REJECT_CUSTOM_ID,
    REMOVE_FIELD_TITLE,
    STATUS_FIELD_TITLE,
    UNAPPROVED_CHANGES_TITLE,
    RoleRequestCodecError,
    build_pending_embed,
    decode_pending_message,
)
from arrest_report_agent.role_requests.discord import (
    ROLE_REQUEST_MODAL_COPY,
    CreateRoleRequestModal,
    PartialApprovalModal,
    RejectRoleRequestModal,
    RoleRequestCog,
    RoleRequestDecisionView,
    RoleRequestService,
    RoleRequestValidationError,
)
from arrest_report_agent.role_requests.models import RoleRequest, RoleRequestStatus

GUILD_ID = 100
CHANNEL_ID = 200
BOT_ID = 300
REQUESTER_ID = 400
REVIEWER_ID = 500
REVIEWER_ROLE_ID = 600


class FakePermissions:
    def __init__(
        self,
        *,
        administrator: bool = False,
        manage_roles: bool = False,
        manage_guild: bool = False,
    ) -> None:
        self.administrator = administrator
        self.manage_roles = manage_roles
        self.manage_guild = manage_guild
        self.view_channel = True
        self.send_messages = True
        self.embed_links = True
        self.read_message_history = True


class FakeRole:
    def __init__(
        self,
        role_id: int,
        *,
        name: str | None = None,
        position: int = 1,
        managed: bool = False,
        permissions: FakePermissions | None = None,
    ) -> None:
        self.id = role_id
        self.name = name or f"Rol {role_id}"
        self.position = position
        self.managed = managed
        self.permissions = permissions or FakePermissions()
        self.guild = None

    @property
    def mention(self) -> str:
        return f"<@&{self.id}>"

    def __ge__(self, other: FakeRole) -> bool:
        return self.position >= other.position


class FakeMember:
    def __init__(
        self,
        member_id: int,
        *,
        roles: list[FakeRole],
        top_role: FakeRole,
        display_name: str = "Taylor Deputy",
    ) -> None:
        self.id = member_id
        self.roles = roles
        self.top_role = top_role
        self.display_name = display_name
        self.display_avatar = SimpleNamespace(url="https://cdn.example/avatar.png")
        self.guild = None
        self.edit_calls: list[dict[str, object]] = []

    async def edit(self, **kwargs) -> None:
        self.edit_calls.append(kwargs)
        self.roles = list(kwargs["roles"])


class FakeChannel:
    def __init__(self, channel_id: int = CHANNEL_ID) -> None:
        self.id = channel_id
        self.name = f"canal-{channel_id}"
        self.mention = f"<#{channel_id}>"
        self.guild = None
        self.messages: dict[int, FakeMessage] = {}
        self.send_calls: list[dict[str, object]] = []

    def permissions_for(self, _member) -> FakePermissions:
        return FakePermissions()

    async def fetch_message(self, message_id: int):
        if message_id not in self.messages:
            raise _not_found()
        return self.messages[message_id]

    async def send(self, **kwargs):
        self.send_calls.append(kwargs)
        message_id = 800 + len(self.messages)
        return FakeMessage(
            message_id=message_id,
            channel=self,
            embed=kwargs["embed"],
        )

    def history(self, **_kwargs):
        async def iterate():
            for message in reversed(tuple(self.messages.values())):
                yield message

        return iterate()


class FakeThread:
    def __init__(self, guild: FakeGuild | None = None) -> None:
        self.id = 999
        self.guild = guild


class FakeGuild:
    def __init__(
        self,
        *,
        channel: FakeChannel,
        requester: FakeMember,
        reviewer: FakeMember,
        roles: list[FakeRole],
        bot_member: FakeMember,
    ) -> None:
        self.id = GUILD_ID
        self.default_role = roles[0]
        self.roles = roles
        self.me = bot_member
        self.text_channels = [channel]
        self._channels = {channel.id: channel}
        self._roles = {role.id: role for role in roles}
        self._members = {requester.id: requester, reviewer.id: reviewer}
        channel.guild = self
        for role in roles:
            role.guild = self
        requester.guild = self
        reviewer.guild = self
        bot_member.guild = self

    def get_channel(self, channel_id: int):
        return self._channels.get(channel_id)

    def get_role(self, role_id: int):
        return self._roles.get(role_id)

    def get_member(self, member_id: int):
        return self._members.get(member_id)

    async def fetch_member(self, member_id: int):
        if member_id not in self._members:
            raise _not_found()
        return self._members[member_id]


class FakeBot:
    def __init__(self, guild: FakeGuild) -> None:
        self.user = SimpleNamespace(
            id=BOT_ID,
            display_avatar=SimpleNamespace(url="https://cdn.example/bot.png"),
        )
        self._guild = guild

    def get_guild(self, guild_id: int):
        return self._guild if guild_id == self._guild.id else None


class FakeMessage:
    def __init__(
        self,
        *,
        message_id: int,
        channel: FakeChannel,
        embed: discord.Embed,
        author_id: int = BOT_ID,
        enabled: bool = True,
    ) -> None:
        self.id = message_id
        self.channel = channel
        self.guild = channel.guild
        self.author = SimpleNamespace(id=author_id)
        self.embeds = [embed]
        self.created_at = datetime(2026, 8, 8, 12, tzinfo=UTC)
        self.components = [_component_row(enabled=enabled)]
        self.edit_calls: list[dict[str, object]] = []
        self.jump_url = f"https://discord.com/channels/{GUILD_ID}/{channel.id}/{message_id}"
        channel.messages[message_id] = self

    async def edit(self, **kwargs) -> None:
        self.edit_calls.append(kwargs)
        if embed := kwargs.get("embed"):
            self.embeds = [embed]
        if view := kwargs.get("view"):
            self.components = [
                SimpleNamespace(
                    children=[
                        SimpleNamespace(
                            custom_id=child.custom_id,
                            disabled=child.disabled,
                        )
                        for child in view.children
                    ]
                )
            ]


class FakeResponse:
    def __init__(self) -> None:
        self.deferred: list[bool] = []
        self.messages: list[dict[str, object]] = []
        self.modals: list[object] = []
        self._done = False

    def is_done(self) -> bool:
        return self._done

    async def defer(self, *, ephemeral: bool) -> None:
        self.deferred.append(ephemeral)
        self._done = True

    async def send_message(self, content: str | None = None, **kwargs) -> None:
        self.messages.append({"content": content, **kwargs})
        self._done = True

    async def send_modal(self, modal) -> None:
        self.modals.append(modal)
        self._done = True


class FakeFollowup:
    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []

    async def send(self, content: str | None = None, **kwargs) -> None:
        self.messages.append({"content": content, **kwargs})


def _not_found() -> discord.NotFound:
    return discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "missing")


def _component_row(*, enabled: bool) -> SimpleNamespace:
    return SimpleNamespace(
        children=[
            SimpleNamespace(custom_id=custom_id, disabled=not enabled)
            for custom_id in (
                APPROVE_ALL_CUSTOM_ID,
                APPROVE_PARTIAL_CUSTOM_ID,
                REJECT_CUSTOM_ID,
            )
        ]
    )


@pytest.fixture
def role_request_env(settings, persona, monkeypatch):
    everyone = FakeRole(GUILD_ID, name="@everyone", position=0)
    add_one = FakeRole(701, name="Investigaciones", position=2)
    add_two = FakeRole(702, name="Instructor", position=3)
    removable = FakeRole(703, name="Patrulla", position=2)
    reviewer_role = FakeRole(REVIEWER_ROLE_ID, name="Supervisión", position=4)
    bot_role = FakeRole(900, name="Bot", position=50)
    requester_top = FakeRole(901, name="Deputy", position=10)
    requester = FakeMember(
        REQUESTER_ID,
        roles=[everyone, removable, requester_top],
        top_role=requester_top,
    )
    reviewer = FakeMember(
        REVIEWER_ID,
        roles=[everyone, reviewer_role],
        top_role=reviewer_role,
        display_name="Sargento Reviewer",
    )
    bot_member = FakeMember(
        BOT_ID,
        roles=[everyone, bot_role],
        top_role=bot_role,
        display_name="Walter Vigil",
    )
    bot_member.guild_permissions = SimpleNamespace(manage_roles=True)
    channel = FakeChannel()
    guild = FakeGuild(
        channel=channel,
        requester=requester,
        reviewer=reviewer,
        roles=[
            everyone,
            add_one,
            add_two,
            removable,
            reviewer_role,
            bot_role,
            requester_top,
        ],
        bot_member=bot_member,
    )
    configured = settings.model_copy(
        update={
            "discord_guild_ids_csv": str(GUILD_ID),
            "role_request_allowed_channel_ids_csv": str(CHANNEL_ID),
            "role_request_reviewer_role_ids_csv": str(REVIEWER_ROLE_ID),
            "role_request_allowed_role_ids_csv": "701,702,703",
        }
    )
    monkeypatch.setattr(
        "arrest_report_agent.role_requests.discord.discord.TextChannel",
        FakeChannel,
    )
    monkeypatch.setattr(
        "arrest_report_agent.role_requests.discord.discord.Member",
        FakeMember,
    )
    service = RoleRequestService(FakeBot(guild), configured, persona)
    return SimpleNamespace(
        service=service,
        guild=guild,
        channel=channel,
        requester=requester,
        reviewer=reviewer,
        add_one=add_one,
        add_two=add_two,
        removable=removable,
        everyone=everyone,
        bot_role=bot_role,
    )


def _pending_message(
    env,
    *,
    message_id: int = 800,
    add_ids=(701,),
    remove_ids=(703,),
    reason: str | None = "Asignación operativa",
):
    embed = build_pending_embed(
        requester=env.requester,
        add_role_ids=add_ids,
        remove_role_ids=remove_ids,
        reason=reason,
        ui=env.service.ui,
        created_at=datetime(2026, 8, 8, 12, tzinfo=UTC),
        message_id=message_id,
    )
    return FakeMessage(message_id=message_id, channel=env.channel, embed=embed)


def _interaction(env, message: FakeMessage, *, user=None):
    return SimpleNamespace(
        guild=env.guild,
        user=user or env.reviewer,
        message=message,
        response=FakeResponse(),
        followup=FakeFollowup(),
    )


def test_role_request_commands_are_root_commands() -> None:
    assert RoleRequestCog.request_roles.name == "solicitar-roles"
    assert RoleRequestCog.info_roles.name == "info-roles"
    assert RoleRequestCog.request_roles.parent is None
    assert RoleRequestCog.info_roles.parent is None


def test_pending_codec_round_trip_supports_add_only_request(role_request_env) -> None:
    message = _pending_message(role_request_env, add_ids=(701,), remove_ids=())

    request = decode_pending_message(
        message,
        bot_user_id=BOT_ID,
        allowed_channel_ids=frozenset({CHANNEL_ID}),
    )

    assert request.requester_id == REQUESTER_ID
    assert request.add_role_ids == (701,)
    assert request.remove_role_ids == ()
    assert request.reason == "Asignación operativa"
    assert [field.name for field in message.embeds[0].fields] == [
        ADD_FIELD_TITLE,
        REASON_FIELD_TITLE,
        STATUS_FIELD_TITLE,
    ]
    assert EMPTY_VALUE not in [field.value for field in message.embeds[0].fields]


def test_pending_codec_accepts_legacy_empty_fields(role_request_env) -> None:
    message = _pending_message(role_request_env, add_ids=(701,), remove_ids=())
    message.embeds[0].set_footer(text=f"Solicitud {message.id} · role-request:v1")
    message.embeds[0].insert_field_at(
        1,
        name=REMOVE_FIELD_TITLE,
        value=EMPTY_VALUE,
        inline=True,
    )

    request = decode_pending_message(
        message,
        bot_user_id=BOT_ID,
        allowed_channel_ids=frozenset({CHANNEL_ID}),
    )

    assert request.add_role_ids == (701,)
    assert request.remove_role_ids == ()


def test_pending_embed_omits_unrequested_additions_and_reason(role_request_env) -> None:
    message = _pending_message(
        role_request_env,
        add_ids=(),
        remove_ids=(703,),
        reason=None,
    )

    request = decode_pending_message(
        message,
        bot_user_id=BOT_ID,
        allowed_channel_ids=frozenset({CHANNEL_ID}),
    )

    assert request.add_role_ids == ()
    assert request.remove_role_ids == (703,)
    assert request.reason is None
    assert [field.name for field in message.embeds[0].fields] == [
        REMOVE_FIELD_TITLE,
        STATUS_FIELD_TITLE,
    ]


@pytest.mark.parametrize("mutation", ["author", "footer", "channel", "buttons"])
def test_pending_codec_rejects_untrusted_or_resolved_messages(
    role_request_env,
    mutation: str,
) -> None:
    message = _pending_message(role_request_env)
    if mutation == "author":
        message.author.id = 999
    elif mutation == "footer":
        message.embeds[0].set_footer(text="Solicitud 999 · role-request:v1")
    elif mutation == "channel":
        allowed = frozenset({999})
    else:
        message.components = [_component_row(enabled=False)]
    allowed = allowed if mutation == "channel" else frozenset({CHANNEL_ID})

    with pytest.raises(RoleRequestCodecError):
        decode_pending_message(
            message,
            bot_user_id=BOT_ID,
            allowed_channel_ids=allowed,
        )


def test_submission_rejects_noops_overlap_and_protected_roles(role_request_env) -> None:
    service = role_request_env.service
    with pytest.raises(RoleRequestValidationError):
        service.validate_submission(role_request_env.requester, (), ())
    with pytest.raises(RoleRequestValidationError):
        service.validate_submission(
            role_request_env.requester,
            (role_request_env.add_one,),
            (role_request_env.add_one,),
        )
    with pytest.raises(RoleRequestValidationError):
        service.validate_submission(
            role_request_env.requester,
            (role_request_env.removable,),
            (),
        )
    with pytest.raises(RoleRequestValidationError):
        service.validate_submission(
            role_request_env.requester,
            (),
            (role_request_env.add_one,),
        )
    with pytest.raises(RoleRequestValidationError):
        service.validate_submission(
            role_request_env.requester,
            (role_request_env.bot_role,),
            (),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("add_role_name", "remove_role_name", "message_template_name"),
    (
        ("removable", None, "already_owned_roles"),
        (None, "add_one", "missing_roles"),
    ),
)
async def test_submission_noops_return_personalized_ephemeral_alerts(
    role_request_env,
    add_role_name: str | None,
    remove_role_name: str | None,
    message_template_name: str,
) -> None:
    interaction = _interaction(role_request_env, SimpleNamespace(id=0))
    add_roles = (getattr(role_request_env, add_role_name),) if add_role_name is not None else ()
    remove_roles = (getattr(role_request_env, remove_role_name),) if remove_role_name is not None else ()

    await role_request_env.service.create_request(
        interaction,
        member=role_request_env.requester,
        channel=role_request_env.channel,
        add_roles=add_roles,
        remove_roles=remove_roles,
        reason=None,
    )

    response = interaction.followup.messages[-1]
    embed = response["embed"]
    selected_role = add_roles[0] if add_roles else remove_roles[0]
    expected = getattr(role_request_env.service.ui, message_template_name).format(roles=selected_role.mention)
    assert response["content"] is None
    assert response["ephemeral"] is True
    assert embed.title == role_request_env.service.ui.alert_title
    assert embed.description == expected
    assert embed.color == discord.Color.red()
    assert not role_request_env.channel.send_calls


def test_submission_allows_high_ranked_requester_for_lower_roles(role_request_env) -> None:
    role_request_env.requester.top_role.position = 100

    additions, removals = role_request_env.service.validate_submission(
        role_request_env.requester,
        (role_request_env.add_one,),
        (),
    )

    assert additions == (role_request_env.add_one,)
    assert removals == ()


@pytest.mark.asyncio
async def test_modals_offer_filtered_role_catalog_and_only_requested_partial_options(
    role_request_env,
) -> None:
    create = CreateRoleRequestModal(
        role_request_env.service,
        member=role_request_env.requester,
        channel=role_request_env.channel,
    )
    request = RoleRequest(
        message_id=800,
        guild_id=GUILD_ID,
        channel_id=CHANNEL_ID,
        requester_id=REQUESTER_ID,
        add_role_ids=(701, 702),
        remove_role_ids=(703,),
        created_at=datetime.now(UTC),
    )
    partial = PartialApprovalModal(
        role_request_env.service,
        request=request,
        guild=role_request_env.guild,
    )
    rejection = RejectRoleRequestModal(role_request_env.service, request)

    assert create.title == ROLE_REQUEST_MODAL_COPY.request_title
    assert len(create.add_selects) == 1
    assert len(create.remove_selects) == 1
    assert [option.value for option in create.add_selects[0].options] == ["701", "702"]
    assert [option.value for option in create.remove_selects[0].options] == ["703"]
    assert create.add_selects[0].max_values == 2
    assert create.remove_selects[0].max_values == 1
    assert create.add_selects[0].required is False
    assert create.add_selects[0].placeholder == (ROLE_REQUEST_MODAL_COPY.add_roles_placeholder)
    assert create.remove_selects[0].placeholder == (ROLE_REQUEST_MODAL_COPY.remove_roles_placeholder)
    assert create.reason_input.placeholder == (ROLE_REQUEST_MODAL_COPY.request_reason_placeholder)
    assert partial.title == ROLE_REQUEST_MODAL_COPY.partial_title
    assert [option.value for option in partial.add_select.options] == ["701", "702"]
    assert [option.value for option in partial.remove_select.options] == ["703"]
    assert partial.add_select.min_values == 0
    assert partial.comment_input.placeholder == (ROLE_REQUEST_MODAL_COPY.partial_comment_placeholder)
    assert rejection.title == ROLE_REQUEST_MODAL_COPY.reject_title
    assert rejection.reason_input.placeholder == (ROLE_REQUEST_MODAL_COPY.reject_reason_placeholder)


@pytest.mark.asyncio
async def test_create_modal_splits_more_than_twenty_five_requestable_roles(
    role_request_env,
) -> None:
    roles = [FakeRole(1_000 + index, name=f"Asignación {index:02d}", position=2) for index in range(26)]
    for role in roles:
        role.guild = role_request_env.guild
        role_request_env.guild._roles[role.id] = role
    configured = role_request_env.service.settings.model_copy(
        update={"role_request_allowed_role_ids_csv": ",".join(str(role.id) for role in roles)}
    )
    service = RoleRequestService(
        role_request_env.service.bot,
        configured,
        role_request_env.service.persona,
    )

    modal = CreateRoleRequestModal(
        service,
        member=role_request_env.requester,
        channel=role_request_env.channel,
    )

    assert [len(select.options) for select in modal.add_selects] == [25, 1]
    assert modal.remove_selects == ()
    assert len(modal.children) == 3


def test_submission_rejects_unlisted_and_more_than_ten_roles(role_request_env) -> None:
    unlisted = FakeRole(999, position=2)
    unlisted.guild = role_request_env.guild
    with pytest.raises(RoleRequestValidationError, match="catálogo"):
        role_request_env.service.validate_submission(
            role_request_env.requester,
            (unlisted,),
            (),
        )

    roles = [FakeRole(1_100 + index, position=2) for index in range(11)]
    for role in roles:
        role.guild = role_request_env.guild
    configured = role_request_env.service.settings.model_copy(
        update={"role_request_allowed_role_ids_csv": ",".join(str(role.id) for role in roles)}
    )
    service = RoleRequestService(
        role_request_env.service.bot,
        configured,
        role_request_env.service.persona,
    )
    with pytest.raises(RoleRequestValidationError, match="10"):
        service.validate_submission(
            role_request_env.requester,
            roles,
            (),
        )


def test_open_role_catalog_uses_every_safe_role_in_the_current_guild(
    role_request_env,
) -> None:
    privileged = FakeRole(
        704,
        name="Administración",
        position=2,
        permissions=FakePermissions(manage_roles=True),
    )
    privileged.guild = role_request_env.guild
    role_request_env.guild.roles.append(privileged)
    role_request_env.guild._roles[privileged.id] = privileged
    configured = role_request_env.service.settings.model_copy(
        update={"role_request_allowed_role_ids_csv": "*"}
    )
    service = RoleRequestService(
        role_request_env.service.bot,
        configured,
        role_request_env.service.persona,
    )

    additions, removals = service.requestable_role_choices(role_request_env.requester)

    assert {role.id for role in additions} == {701, 702}
    assert {role.id for role in removals} == {703, 901}
    assert privileged not in additions


def test_open_role_catalog_accepts_safe_selected_roles(role_request_env) -> None:
    configured = role_request_env.service.settings.model_copy(
        update={"role_request_allowed_role_ids_csv": ""}
    )
    service = RoleRequestService(
        role_request_env.service.bot,
        configured,
        role_request_env.service.persona,
    )

    additions, removals = service.validate_submission(
        role_request_env.requester,
        (role_request_env.add_one,),
        (role_request_env.removable,),
    )
    resolved = service.resolve_selected_roles(
        role_request_env.guild,
        (str(role_request_env.add_one.id), str(role_request_env.removable.id)),
    )

    assert additions == (role_request_env.add_one,)
    assert removals == (role_request_env.removable,)
    assert resolved == (role_request_env.add_one, role_request_env.removable)


@pytest.mark.asyncio
async def test_unauthorized_reviewer_cannot_open_decision_modals(role_request_env) -> None:
    message = _pending_message(role_request_env)
    interaction = _interaction(
        role_request_env,
        message,
        user=role_request_env.requester,
    )
    view = RoleRequestDecisionView(role_request_env.service)

    allowed = await view.interaction_check(interaction)

    assert allowed is False
    assert interaction.response.messages[0]["ephemeral"] is True
    assert not interaction.response.modals


@pytest.mark.asyncio
async def test_full_approval_applies_roles_once_and_disables_buttons(role_request_env) -> None:
    message = _pending_message(role_request_env)
    interaction = _interaction(role_request_env, message)

    await role_request_env.service.approve_all(interaction)

    assert interaction.response.deferred == [True]
    assert len(role_request_env.requester.edit_calls) == 1
    role_ids = {role.id for role in role_request_env.requester.roles}
    assert 701 in role_ids
    assert 703 not in role_ids
    assert all(child.disabled for child in message.components[0].children)
    assert message.embeds[0].title == role_request_env.service.ui.status_approved
    approval_notice = interaction.followup.messages[-1]
    assert approval_notice["content"] is None
    assert approval_notice["ephemeral"] is True
    assert approval_notice["embed"].title == role_request_env.service.ui.status_approved
    assert approval_notice["embed"].color == discord.Color.green()

    second = _interaction(role_request_env, message)
    await role_request_env.service.approve_all(second)
    assert len(role_request_env.requester.edit_calls) == 1
    assert role_request_env.service.ui.stale_request in second.followup.messages[-1]["embed"].description


@pytest.mark.asyncio
async def test_decision_embed_omits_empty_request_and_change_sections(
    role_request_env,
) -> None:
    message = _pending_message(
        role_request_env,
        add_ids=(701,),
        remove_ids=(),
        reason=None,
    )
    interaction = _interaction(role_request_env, message)

    await role_request_env.service.approve_all(interaction)

    fields = {field.name: field.value for field in message.embeds[0].fields}
    assert REMOVE_FIELD_TITLE not in fields
    assert REASON_FIELD_TITLE not in fields
    assert "<@&701>" in fields[APPROVED_CHANGES_TITLE]
    assert "Retirar" not in fields[APPROVED_CHANGES_TITLE]
    assert EMPTY_VALUE not in fields.values()


@pytest.mark.asyncio
async def test_full_approval_accepts_changes_already_applied_manually(
    role_request_env,
) -> None:
    message = _pending_message(role_request_env)
    interaction = _interaction(role_request_env, message)
    role_request_env.requester.roles = [
        role_request_env.everyone,
        role_request_env.add_one,
        role_request_env.requester.top_role,
    ]

    await role_request_env.service.approve_all(interaction)

    assert not role_request_env.requester.edit_calls
    assert message.embeds[0].title == role_request_env.service.ui.status_approved


@pytest.mark.asyncio
async def test_partial_approval_applies_subset_and_closes_omitted_changes(
    role_request_env,
) -> None:
    message = _pending_message(role_request_env, add_ids=(701, 702), remove_ids=(703,))
    interaction = _interaction(role_request_env, message)

    await role_request_env.service.approve_partial(
        interaction,
        guild_id=GUILD_ID,
        channel_id=CHANNEL_ID,
        message_id=message.id,
        selected_add=(701,),
        selected_remove=(),
        comment="Se autoriza solo Investigaciones",
    )

    role_ids = {role.id for role in role_request_env.requester.roles}
    assert 701 in role_ids
    assert 702 not in role_ids
    assert 703 in role_ids
    assert message.embeds[0].title == role_request_env.service.ui.status_partial
    fields = {field.name: field.value for field in message.embeds[0].fields}
    assert "<@&702>" in fields[UNAPPROVED_CHANGES_TITLE]
    assert "<@&703>" in fields[UNAPPROVED_CHANGES_TITLE]
    assert EMPTY_VALUE not in fields.values()
    partial_notice = interaction.followup.messages[-1]
    assert partial_notice["content"] is None
    assert partial_notice["embed"].title == role_request_env.service.ui.status_partial
    assert partial_notice["embed"].color == discord.Color.gold()


@pytest.mark.asyncio
async def test_rejection_keeps_roles_and_records_terminal_result(role_request_env) -> None:
    message = _pending_message(role_request_env)
    interaction = _interaction(role_request_env, message)
    original = [role.id for role in role_request_env.requester.roles]

    await role_request_env.service.reject(
        interaction,
        guild_id=GUILD_ID,
        channel_id=CHANNEL_ID,
        message_id=message.id,
        reason="Asignación no autorizada",
    )

    assert [role.id for role in role_request_env.requester.roles] == original
    assert not role_request_env.requester.edit_calls
    assert message.embeds[0].title == role_request_env.service.ui.status_rejected
    assert all(child.disabled for child in message.components[0].children)
    rejection_notice = interaction.followup.messages[-1]
    assert rejection_notice["content"] is None
    assert rejection_notice["embed"].title == role_request_env.service.ui.status_rejected
    assert rejection_notice["embed"].color == discord.Color.red()


@pytest.mark.asyncio
async def test_pending_scan_finds_request_across_visible_allowed_channels(
    role_request_env,
) -> None:
    message = _pending_message(role_request_env)

    found = await role_request_env.service.find_pending_request(
        role_request_env.guild,
        REQUESTER_ID,
    )

    assert found is message


@pytest.mark.asyncio
async def test_creation_publishes_in_origin_channel_and_blocks_second_pending_request(
    role_request_env,
) -> None:
    first = _interaction(role_request_env, SimpleNamespace(id=0))

    await role_request_env.service.create_request(
        first,
        member=role_request_env.requester,
        channel=role_request_env.channel,
        add_roles=(role_request_env.add_one,),
        remove_roles=(),
        reason=None,
    )

    assert first.response.deferred == [True]
    assert len(role_request_env.channel.send_calls) == 1
    request_message = next(iter(role_request_env.channel.messages.values()))
    assert request_message.embeds[0].footer.text == f"Solicitud {request_message.id}"
    assert all(not child.disabled for child in request_message.components[0].children)
    creation_notice = first.followup.messages[-1]
    assert creation_notice["content"] is None
    assert creation_notice["embed"].title == role_request_env.service.ui.pending_title
    assert "http" in creation_notice["embed"].description
    assert creation_notice["embed"].color == discord.Color.gold()

    second = _interaction(role_request_env, SimpleNamespace(id=0))
    await role_request_env.service.create_request(
        second,
        member=role_request_env.requester,
        channel=role_request_env.channel,
        add_roles=(role_request_env.add_two,),
        remove_roles=(),
        reason=None,
    )

    assert len(role_request_env.channel.send_calls) == 1
    assert "http" in second.followup.messages[-1]["embed"].description


@pytest.mark.asyncio
async def test_concurrent_creation_keeps_one_pending_request(role_request_env) -> None:
    first = _interaction(role_request_env, SimpleNamespace(id=0))
    second = _interaction(role_request_env, SimpleNamespace(id=0))

    await asyncio.gather(
        role_request_env.service.create_request(
            first,
            member=role_request_env.requester,
            channel=role_request_env.channel,
            add_roles=(role_request_env.add_one,),
            remove_roles=(),
            reason=None,
        ),
        role_request_env.service.create_request(
            second,
            member=role_request_env.requester,
            channel=role_request_env.channel,
            add_roles=(role_request_env.add_two,),
            remove_roles=(),
            reason=None,
        ),
    )

    assert len(role_request_env.channel.send_calls) == 1
    assert len(role_request_env.channel.messages) == 1
    assert all(interaction.followup.messages for interaction in (first, second))


@pytest.mark.asyncio
async def test_request_command_does_not_apply_agent_user_allowlist(
    role_request_env,
) -> None:
    restricted = role_request_env.service.settings.model_copy(
        update={"authorized_user_ids_csv": "999999"},
    )
    service = RoleRequestService(
        role_request_env.service.bot,
        restricted,
        role_request_env.service.persona,
    )
    modals: list[object] = []

    async def send_modal(modal) -> None:
        modals.append(modal)

    async def unexpected_response(*_args, **_kwargs) -> None:
        raise AssertionError("an agent user allowlist must not block role requests")

    ctx = SimpleNamespace(
        guild=role_request_env.guild,
        author=role_request_env.requester,
        channel=role_request_env.channel,
        respond=unexpected_response,
        send_modal=send_modal,
    )
    cog = RoleRequestCog(service)

    await cog.request_roles.callback(cog, ctx)

    assert len(modals) == 1
    assert isinstance(modals[0], CreateRoleRequestModal)


@pytest.mark.asyncio
async def test_request_command_blocks_threads_but_info_remains_available(
    role_request_env,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "arrest_report_agent.role_requests.discord.discord.Thread",
        FakeThread,
    )
    responses: list[dict[str, object]] = []
    modals: list[object] = []

    async def respond(content=None, **kwargs) -> None:
        responses.append({"content": content, **kwargs})

    async def send_modal(modal) -> None:
        modals.append(modal)

    ctx = SimpleNamespace(
        guild=role_request_env.guild,
        author=role_request_env.requester,
        channel=FakeThread(role_request_env.guild),
        respond=respond,
        send_modal=send_modal,
    )
    cog = RoleRequestCog(role_request_env.service)

    await cog.request_roles.callback(cog, ctx)
    assert role_request_env.service.ui.thread_blocked in responses[-1]["embed"].description
    assert not modals

    await cog.info_roles.callback(cog, ctx)
    assert responses[-1]["ephemeral"] is False
    assert responses[-1]["embed"].title == role_request_env.service.ui.info_title
    assert responses[-1]["embed"].footer is None


@pytest.mark.asyncio
async def test_concurrent_decisions_apply_at_most_once(role_request_env) -> None:
    message = _pending_message(role_request_env)
    first = _interaction(role_request_env, message)
    second = _interaction(role_request_env, message)

    await asyncio.gather(
        role_request_env.service.approve_all(first),
        role_request_env.service.approve_all(second),
    )

    assert len(role_request_env.requester.edit_calls) == 1
    outcomes = [followup.messages[-1] for followup in (first.followup, second.followup)]
    assert any(
        outcome.get("embed") is not None
        and outcome["embed"].title == role_request_env.service.ui.status_approved
        for outcome in outcomes
    )
    assert any(
        outcome.get("embed") is not None
        and outcome["embed"].description == role_request_env.service.ui.stale_request
        for outcome in outcomes
    )


@pytest.mark.asyncio
async def test_transient_discord_failure_leaves_request_pending(role_request_env) -> None:
    message = _pending_message(role_request_env)
    interaction = _interaction(role_request_env, message)

    async def fail_edit(**_kwargs) -> None:
        raise discord.HTTPException(
            SimpleNamespace(status=500, reason="Server Error"),
            "temporary",
        )

    role_request_env.requester.edit = fail_edit

    await role_request_env.service.approve_all(interaction)

    assert all(not child.disabled for child in message.components[0].children)
    assert message.embeds[0].title == role_request_env.service.ui.pending_title
    response = interaction.followup.messages[-1]
    assert response["content"] is None
    assert response["embed"].description == role_request_env.service.ui.operation_failed


@pytest.mark.asyncio
async def test_deleted_role_closes_request_as_failed(role_request_env) -> None:
    message = _pending_message(role_request_env)
    interaction = _interaction(role_request_env, message)
    del role_request_env.guild._roles[701]

    await role_request_env.service.approve_all(interaction)

    assert not role_request_env.requester.edit_calls
    assert message.embeds[0].title == role_request_env.service.ui.status_failed
    assert all(child.disabled for child in message.components[0].children)


@pytest.mark.asyncio
async def test_persona_copy_drives_buttons_and_command_descriptions(
    role_request_env,
) -> None:
    view = RoleRequestDecisionView(role_request_env.service)
    cog = RoleRequestCog(role_request_env.service)

    assert [child.label for child in view.children] == [
        role_request_env.service.ui.approve_all_button,
        role_request_env.service.ui.approve_partial_button,
        role_request_env.service.ui.reject_button,
    ]
    assert cog.request_roles.description == (
        role_request_env.service.persona.messages.interface.commands.request_roles_description
    )
    assert cog.info_roles.description == (
        role_request_env.service.persona.messages.interface.commands.info_roles_description
    )


def test_role_request_status_is_terminal_after_each_decision() -> None:
    assert set(RoleRequestStatus) == {
        RoleRequestStatus.PENDING,
        RoleRequestStatus.APPROVED,
        RoleRequestStatus.PARTIALLY_APPROVED,
        RoleRequestStatus.REJECTED,
        RoleRequestStatus.FAILED,
    }

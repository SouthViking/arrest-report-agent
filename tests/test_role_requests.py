from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import discord
import pytest

from arrest_report_agent.config import (
    RoleRequestFttEntryOptions,
    RoleRequestFttWelcomeOptions,
    RoleRequestOptions,
    RoleRequestShiftOption,
    RoleRequestStationOption,
)
from arrest_report_agent.role_requests.codec import (
    ADD_FIELD_TITLE,
    APPROVE_ALL_CUSTOM_ID,
    APPROVE_PARTIAL_CUSTOM_ID,
    APPROVED_CHANGES_TITLE,
    EMPTY_VALUE,
    FTT_ENTRY_PENDING_TITLE,
    REASON_FIELD_TITLE,
    REJECT_CUSTOM_ID,
    REMOVE_FIELD_TITLE,
    REQUEST_KIND_FIELD_TITLE,
    REVIEWER_FIELD_TITLE,
    SHIFT_FIELD_TITLE,
    STATION_FIELD_TITLE,
    STATUS_FIELD_TITLE,
    UNAPPROVED_CHANGES_TITLE,
    RoleRequestCodecError,
    build_pending_embed,
    build_private_pending_embed,
    build_public_decision_embed,
    build_public_pending_embed,
    decode_pending_message,
)
from arrest_report_agent.role_requests.discord import (
    ROLE_REQUEST_MODAL_COPY,
    CreateRoleRequestModal,
    FttEntryApprovalModal,
    FttEntryRoleRequestModal,
    PartialApprovalModal,
    RejectRoleRequestModal,
    RetirementRoleRequestModal,
    RoleRequestCog,
    RoleRequestDecisionView,
    RoleRequestResolutionView,
    RoleRequestService,
    RoleRequestServiceRegistry,
    RoleRequestStartView,
    RoleRequestValidationError,
    build_ftt_entry_welcome_embed,
)
from arrest_report_agent.role_requests.models import (
    RoleRequest,
    RoleRequestDecision,
    RoleRequestKind,
    RoleRequestReviewMessage,
    RoleRequestShiftChange,
    RoleRequestStatus,
    StoredRoleRequest,
    StoredRoleRequestStatus,
)

GUILD_ID = 100
CHANNEL_ID = 200
BOT_ID = 300
REQUESTER_ID = 400
REVIEWER_ID = 500
REVIEWER_ROLE_ID = 600
WELCOME_CHANNEL_ID = 201
ANNOUNCEMENTS_CHANNEL_ID = 202
BIG_BROTHER_CHANNEL_ID = 203
INFORMATION_CHANNEL_ID = 204
QUESTIONS_CHANNEL_ID = 205


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
        self.attach_files = False  # Match #pedir-roles: roles do not need file uploads.
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
        self.bot = False
        self.roles = roles
        self.top_role = top_role
        self.display_name = display_name
        self.display_avatar = SimpleNamespace(url="https://cdn.example/avatar.png")
        self.guild = None
        self.edit_calls: list[dict[str, object]] = []
        self.dm_calls: list[dict[str, object]] = []
        self.dm_blocked = False

    @property
    def mention(self) -> str:
        return f"<@{self.id}>"

    async def edit(self, **kwargs) -> None:
        self.edit_calls.append(kwargs)
        self.roles = list(kwargs["roles"])

    async def send(self, **kwargs) -> None:
        if self.dm_blocked:
            raise discord.Forbidden(
                SimpleNamespace(status=403, reason="Forbidden"),
                "DMs disabled",
            )
        self.dm_calls.append(kwargs)


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
            file=kwargs.get("file"),
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

    async def fetch_channel(self, channel_id: int):
        if channel_id not in self._channels:
            raise _not_found()
        return self._channels[channel_id]


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
        file: discord.File | None = None,
    ) -> None:
        self.id = message_id
        self.channel = channel
        self.guild = channel.guild
        self.author = SimpleNamespace(id=author_id)
        self.embeds = [embed]
        self.attachments = [SimpleNamespace(filename=file.filename)] if file else []
        self.created_at = datetime(2026, 8, 8, 12, tzinfo=UTC)
        self.components = [_component_row(enabled=enabled)]
        self.edit_calls: list[dict[str, object]] = []
        self.jump_url = f"https://discord.com/channels/{GUILD_ID}/{channel.id}/{message_id}"
        channel.messages[message_id] = self
        self.deleted = False

    async def edit(self, **kwargs) -> None:
        self.edit_calls.append(kwargs)
        if file := kwargs.get("file"):
            self.attachments.append(SimpleNamespace(filename=file.filename))
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

    async def delete(self) -> None:
        self.deleted = True
        self.channel.messages.pop(self.id, None)


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


class FakeRoleRequestRepository:
    def __init__(self) -> None:
        self.request: StoredRoleRequest | None = None
        self.mirrors: tuple[RoleRequestReviewMessage, ...] = ()
        self.dm_delivered: bool | None = None
        self.history: dict[object, StoredRoleRequest] = {}
        self.historical_mirrors: dict[object, tuple[RoleRequestReviewMessage, ...]] = {}
        self.cancellation_due: dict[object, datetime | None] = {}

    async def reserve(self, **values) -> tuple[StoredRoleRequest, bool]:
        now = datetime.now(UTC)
        if self.request is not None:
            self.history[self.request.id] = self.request
            self.historical_mirrors[self.request.id] = self.mirrors
        self.request = StoredRoleRequest(
            id=uuid4(),
            guild_id=values["guild_id"],
            interaction_id=values["interaction_id"],
            requester_id=values["requester_id"],
            kind=values["kind"],
            shift_change=values.get("shift_change"),
            add_role_ids=values["add_role_ids"],
            remove_role_ids=values["remove_role_ids"],
            reason=values["reason"],
            status=StoredRoleRequestStatus.PUBLISHING,
            public_channel_id=values["public_channel_id"],
            version=1,
            created_at=now,
            updated_at=now,
        )
        return self.request, True

    async def mark_published(
        self,
        request_id,
        *,
        public_message_id: int,
        review_messages: tuple[tuple[int, int], ...],
    ) -> StoredRoleRequest:
        assert self.request is not None and self.request.id == request_id
        now = datetime.now(UTC)
        for key, previous in tuple(self.history.items()):
            if previous.status is StoredRoleRequestStatus.PENDING:
                self.history[key] = previous.model_copy(update={
                    "status": StoredRoleRequestStatus.CANCELLED,
                    "replaced_by_request_id": request_id, "cancellation_sync_due": now,
                })
        self.request = self.request.model_copy(
            update={
                "public_message_id": public_message_id,
                "status": StoredRoleRequestStatus.PENDING,
                "version": 2,
                "updated_at": now,
            }
        )
        self.mirrors = tuple(
            RoleRequestReviewMessage(
                request_id=request_id,
                channel_id=channel_id,
                message_id=message_id,
                created_at=now,
                updated_at=now,
            )
            for channel_id, message_id in review_messages
        )
        return self.request

    async def get(self, request_id):
        if self.request and self.request.id == request_id:
            return self.request
        return self.history.get(request_id)

    async def list_cancellation_sync(self, *, replacement_id=None):
        return tuple(request for request in self.history.values()
                     if request.status is StoredRoleRequestStatus.CANCELLED
                     and request.cancellation_sync_due is not None
                     and (replacement_id is None or request.replaced_by_request_id == replacement_id))

    async def set_cancellation_sync_due(self, request_id, due):
        self.cancellation_due[request_id] = due
        self.history[request_id] = self.history[request_id].model_copy(update={"cancellation_sync_due": due})

    async def mark_failed(self, _request_id) -> None:
        if self.request is not None:
            self.request = self.request.model_copy(update={"status": StoredRoleRequestStatus.FAILED})

    async def get_by_review_message(self, guild_id: int, channel_id: int, message_id: int):
        for request_id, mirrors in self.historical_mirrors.items():
            previous = self.history[request_id]
            if previous.guild_id == guild_id and any(
                mirror.channel_id == channel_id and mirror.message_id == message_id for mirror in mirrors
            ):
                return previous
        if self.request is None or self.request.guild_id != guild_id:
            return None
        return (
            self.request
            if any(
                mirror.channel_id == channel_id and mirror.message_id == message_id for mirror in self.mirrors
            )
            else None
        )

    async def get_by_public_message(self, guild_id: int, channel_id: int, message_id: int):
        if self.request is None:
            return None
        if (
            self.request.guild_id != guild_id
            or self.request.public_channel_id != channel_id
            or self.request.public_message_id != message_id
        ):
            return None
        return self.request

    async def claim_decision(self, request_id):
        if (
            self.request is None
            or self.request.id != request_id
            or self.request.status is not StoredRoleRequestStatus.PENDING
        ):
            return None
        self.request = self.request.model_copy(update={"status": StoredRoleRequestStatus.PROCESSING})
        return self.request

    async def release_decision(self, request_id) -> None:
        if self.request is not None and self.request.id == request_id:
            self.request = self.request.model_copy(update={"status": StoredRoleRequestStatus.PENDING})

    async def finalize(self, request_id, decision: RoleRequestDecision):
        assert self.request is not None and self.request.id == request_id
        self.request = self.request.model_copy(
            update={
                "status": StoredRoleRequestStatus(decision.status.value),
                "reviewer_id": decision.reviewer_id,
                "decision_comment": decision.comment,
                "approved_add_role_ids": decision.approved_add_role_ids,
                "approved_remove_role_ids": decision.approved_remove_role_ids,
                "unapproved_add_role_ids": decision.unapproved_add_role_ids,
                "unapproved_remove_role_ids": decision.unapproved_remove_role_ids,
                "decided_at": decision.decided_at,
            }
        )
        return self.request

    async def review_messages(self, request_id):
        return self.historical_mirrors.get(request_id, self.mirrors)

    async def mark_dm_delivery(self, _request_id, delivered: bool) -> None:
        self.dm_delivered = delivered


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
        id=9900,
        guild=env.guild,
        channel=getattr(message, "channel", env.channel),
        user=user or env.reviewer,
        message=message,
        response=FakeResponse(),
        followup=FakeFollowup(),
    )


def _select_modal_values(select, values):
    select.refresh_from_modal(SimpleNamespace(data={}), {"values": values})


def _retirement_service(env, retired_role_id: int = 704) -> tuple[RoleRequestService, FakeRole]:
    retired = FakeRole(retired_role_id, name="Retired Employee", position=2)
    retired.guild = env.guild
    env.guild.roles.append(retired)
    env.guild._roles[retired.id] = retired
    configured = env.service.settings.model_copy(
        update={
            "role_request_allowed_role_ids_csv": f"701,702,703,{retired.id}",
            "role_request_retired_role_id": retired.id,
        }
    )
    service = RoleRequestService(env.service.bot, configured, env.service.persona)
    service.operational_staff_role_ids = frozenset({env.requester.top_role.id})
    return service, retired


def _ftt_entry_service(env) -> tuple[RoleRequestService, tuple[FakeRole, ...]]:
    roles = (
        FakeRole(710, name="Field Training Trainee", position=2),
        FakeRole(711, name="Deputy Sheriff", position=3),
        FakeRole(712, name="Field Staff", position=4),
        FakeRole(713, name="CSD: TTCF", position=5),
        FakeRole(714, name="CPD: GNT", position=6),
        FakeRole(715, name="NPD: DLM", position=6),
    )
    for role in roles:
        role.guild = env.guild
        env.guild.roles.append(role)
        env.guild._roles[role.id] = role
    configured = env.service.settings.model_copy(
        update={
            "role_request_allowed_role_ids_csv": ",".join(str(role.id) for role in roles),
        }
    )
    welcome_channel = FakeChannel(WELCOME_CHANNEL_ID)
    welcome_channel.guild = env.guild
    env.guild._channels[WELCOME_CHANNEL_ID] = welcome_channel
    env.guild.text_channels.append(welcome_channel)
    service = RoleRequestService(env.service.bot, configured, env.service.persona)
    service.operational_staff_role_ids = frozenset({712})
    service.ftt_entry_options = RoleRequestFttEntryOptions(
        base_role_ids=(710, 711, 712, 713),
        stations=(
            RoleRequestStationOption(
                key="ganton",
                label="Ganton",
                role_id=714,
                emoji="🏙️",
            ),
            RoleRequestStationOption(
                key="dillimore",
                label="Dillimore",
                role_id=715,
                emoji="🌲",
            ),
        ),
        welcome=RoleRequestFttWelcomeOptions(
            channel_id=WELCOME_CHANNEL_ID,
            rules_url="https://sheriff.sarp.es/viewtopic.php?t=37",
            reference_channel_ids=(ANNOUNCEMENTS_CHANNEL_ID, BIG_BROTHER_CHANNEL_ID),
            information_channel_id=INFORMATION_CHANNEL_ID,
            questions_channel_id=QUESTIONS_CHANNEL_ID,
        ),
    )
    return service, roles


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


def test_v2_public_embed_hides_sensitive_fields_and_private_embed_keeps_them(
    role_request_env,
) -> None:
    created_at = datetime(2026, 8, 21, 12, tzinfo=UTC)
    public = build_public_pending_embed(
        requester=role_request_env.requester,
        add_role_ids=(701,),
        remove_role_ids=(703,),
        ui=role_request_env.service.ui,
        created_at=created_at,
        kind=RoleRequestKind.NORMAL,
    )
    private = build_private_pending_embed(
        requester=role_request_env.requester,
        add_role_ids=(701,),
        remove_role_ids=(703,),
        reason="Motivo reservado",
        ui=role_request_env.service.ui,
        created_at=created_at,
        public_jump_url="https://discord.com/channels/100/200/300",
        kind=RoleRequestKind.NORMAL,
    )
    assert REASON_FIELD_TITLE not in {field.name for field in public.fields}
    assert "Motivo reservado" not in str(public.to_dict())
    assert "Motivo reservado" in str(private.to_dict())

    request = RoleRequest(
        message_id=300,
        guild_id=GUILD_ID,
        channel_id=CHANNEL_ID,
        requester_id=REQUESTER_ID,
        add_role_ids=(701,),
        remove_role_ids=(703,),
        reason="Motivo reservado",
        created_at=created_at,
    )
    decision = RoleRequestDecision(
        status=RoleRequestStatus.REJECTED,
        reviewer_id=REVIEWER_ID,
        unapproved_add_role_ids=(701,),
        unapproved_remove_role_ids=(703,),
        comment="Comentario privado",
        decided_at=created_at,
    )
    terminal = build_public_decision_embed(
        request,
        decision,
        requester_name="Persona",
        requester_avatar_url=None,
        ui=role_request_env.service.ui,
    )
    terminal_names = {field.name for field in terminal.fields}
    assert REASON_FIELD_TITLE not in terminal_names
    assert REVIEWER_FIELD_TITLE not in terminal_names
    assert "Comentario privado" not in str(terminal.to_dict())


def test_v2_public_ftt_entry_embed_keeps_station_but_has_no_private_reason(
    role_request_env,
) -> None:
    embed = build_public_pending_embed(
        requester=role_request_env.requester,
        add_role_ids=(710, 711, 712, 713, 714),
        remove_role_ids=(),
        ui=role_request_env.service.ui,
        created_at=datetime(2026, 9, 21, 12, tzinfo=UTC),
        kind=RoleRequestKind.FTT_ENTRY,
        reason="Ganton · <@&714>",
    )

    fields = {field.name: field.value for field in embed.fields}
    assert embed.title == FTT_ENTRY_PENDING_TITLE
    assert fields[STATION_FIELD_TITLE] == "Ganton · <@&714>"
    assert REASON_FIELD_TITLE not in fields


@pytest.mark.asyncio
@pytest.mark.parametrize("dm_blocked", (False, True))
async def test_v2_request_publishes_public_state_and_two_private_review_mirrors(
    role_request_env,
    dm_blocked: bool,
) -> None:
    repository = FakeRoleRequestRepository()
    service = role_request_env.service
    service.repository = repository  # type: ignore[assignment]
    first_review = FakeChannel(210)
    second_review = FakeChannel(211)
    for channel in (first_review, second_review):
        channel.guild = role_request_env.guild
        role_request_env.guild._channels[channel.id] = channel
        role_request_env.guild.text_channels.append(channel)
    service.review_channel_ids = (first_review.id, second_review.id)
    role_request_env.requester.dm_blocked = dm_blocked
    interaction = SimpleNamespace(
        id=9910,
        response=FakeResponse(),
        followup=FakeFollowup(),
    )

    await service.create_request(
        interaction,
        member=role_request_env.requester,
        channel=role_request_env.channel,
        add_roles=(role_request_env.add_one,),
        remove_roles=(role_request_env.removable,),
        reason="Motivo reservado",
    )

    assert len(role_request_env.channel.send_calls) == 1
    assert "view" not in role_request_env.channel.send_calls[0]
    public_embed = role_request_env.channel.send_calls[0]["embed"]
    assert "Motivo reservado" not in str(public_embed.to_dict())
    assert len(first_review.send_calls) == len(second_review.send_calls) == 1
    assert first_review.send_calls[0]["content"] == f"<@&{REVIEWER_ROLE_ID}>"
    assert second_review.send_calls[0]["content"] == f"<@&{REVIEWER_ROLE_ID}>"
    assert "Motivo reservado" in str(first_review.send_calls[0]["embed"].to_dict())
    assert first_review.send_calls[0]["view"] is not None
    assert repository.request is not None
    assert repository.request.status is StoredRoleRequestStatus.PENDING
    assert len(repository.mirrors) == 2

    private_message = next(iter(first_review.messages.values()))
    decision = _interaction(role_request_env, private_message)
    await service.approve_all(decision)

    assert repository.request.status is StoredRoleRequestStatus.APPROVED
    assert repository.dm_delivered is not dm_blocked
    assert len(role_request_env.requester.dm_calls) == (0 if dm_blocked else 1)
    assert not decision.followup.messages
    public_message = next(iter(role_request_env.channel.messages.values()))
    public_rendered = str(public_message.embeds[0].to_dict())
    assert "Motivo reservado" not in public_rendered
    assert f"<@{REVIEWER_ID}>" not in public_rendered
    final_public_view = public_message.edit_calls[-1]["view"]
    if dm_blocked:
        assert isinstance(final_public_view, RoleRequestResolutionView)
    else:
        assert final_public_view is None
    for review_channel in (first_review, second_review):
        mirror = next(iter(review_channel.messages.values()))
        assert all(child.disabled for child in mirror.components[0].children)
        assert f"<@{REVIEWER_ID}>" in str(mirror.embeds[0].to_dict())


@pytest.mark.asyncio
@pytest.mark.parametrize("viewer_kind", ("requester", "reviewer", "outsider"))
async def test_resolution_is_visible_only_to_requester_or_authorized_reviewer(
    role_request_env,
    viewer_kind: str,
) -> None:
    repository = FakeRoleRequestRepository()
    service = role_request_env.service
    service.repository = repository  # type: ignore[assignment]
    review_channel = FakeChannel(210)
    review_channel.guild = role_request_env.guild
    role_request_env.guild._channels[review_channel.id] = review_channel
    role_request_env.guild.text_channels.append(review_channel)
    service.review_channel_ids = (review_channel.id,)
    role_request_env.requester.dm_blocked = True
    creation = SimpleNamespace(id=9910, response=FakeResponse(), followup=FakeFollowup())
    await service.create_request(
        creation,
        member=role_request_env.requester,
        channel=role_request_env.channel,
        add_roles=(role_request_env.add_one,),
        remove_roles=(role_request_env.removable,),
        reason="Motivo reservado",
    )
    private_message = next(iter(review_channel.messages.values()))
    await service.approve_all(_interaction(role_request_env, private_message))
    public_message = next(iter(role_request_env.channel.messages.values()))
    if viewer_kind == "requester":
        viewer = role_request_env.requester
    elif viewer_kind == "reviewer":
        viewer = role_request_env.reviewer
    else:
        viewer = FakeMember(
            999,
            roles=[role_request_env.everyone],
            top_role=role_request_env.everyone,
            display_name="Sin acceso",
        )
        viewer.guild = role_request_env.guild
    interaction = _interaction(role_request_env, public_message, user=viewer)
    view = RoleRequestResolutionView(service)

    await view.children[0].callback(interaction)

    response = interaction.response.messages[-1]
    assert response["ephemeral"] is True
    if viewer_kind == "outsider":
        assert response["embed"].description == service.ui.reviewer_unauthorized
    else:
        assert response["embed"].description != service.ui.reviewer_unauthorized
        assert response["embed"].title != service.ui.alert_title
        assert APPROVED_CHANGES_TITLE in {field.name for field in response["embed"].fields}
        assert not response["embed"].thumbnail
        assert "file" not in response


def test_pending_codec_round_trip_supports_retirement(role_request_env) -> None:
    service, retired = _retirement_service(role_request_env)
    embed = build_pending_embed(
        requester=role_request_env.requester,
        add_role_ids=(retired.id,),
        remove_role_ids=(role_request_env.removable.id,),
        reason=None,
        ui=service.ui,
        created_at=datetime(2026, 8, 8, 12, tzinfo=UTC),
        message_id=850,
        kind=RoleRequestKind.RETIREMENT,
    )
    message = FakeMessage(message_id=850, channel=role_request_env.channel, embed=embed)
    message.components[0].children[1].disabled = True

    request = decode_pending_message(
        message,
        bot_user_id=BOT_ID,
        allowed_channel_ids=frozenset({CHANNEL_ID}),
    )

    assert request.kind is RoleRequestKind.RETIREMENT
    assert request.add_role_ids == (retired.id,)
    assert message.embeds[0].fields[0].name == REQUEST_KIND_FIELD_TITLE


def test_retirement_codec_accepts_legacy_disabled_partial_button(role_request_env) -> None:
    service, retired = _retirement_service(role_request_env)
    embed = build_pending_embed(
        requester=role_request_env.requester,
        add_role_ids=(retired.id,),
        remove_role_ids=(703,),
        reason=None,
        ui=service.ui,
        created_at=datetime.now(UTC),
        message_id=851,
        kind=RoleRequestKind.RETIREMENT,
    )
    message = FakeMessage(message_id=851, channel=role_request_env.channel, embed=embed)
    message.components[0].children[1].disabled = True
    request = decode_pending_message(
        message,
        bot_user_id=BOT_ID,
        allowed_channel_ids=frozenset({CHANNEL_ID}),
    )
    assert request.kind is RoleRequestKind.RETIREMENT


def test_retirement_codec_accepts_previous_request_description(role_request_env) -> None:
    service, retired = _retirement_service(role_request_env)
    embed = build_pending_embed(
        requester=role_request_env.requester,
        add_role_ids=(retired.id,),
        remove_role_ids=(703,),
        reason=None,
        ui=service.ui,
        created_at=datetime.now(UTC),
        message_id=852,
        kind=RoleRequestKind.RETIREMENT,
    )
    for field in embed.fields:
        if field.name == REQUEST_KIND_FIELD_TITLE:
            field.value = "Retiro de la facción · se retirarán los roles gestionados actuales."
    message = FakeMessage(message_id=852, channel=role_request_env.channel, embed=embed)
    message.components[0].children[1].disabled = True

    request = decode_pending_message(
        message,
        bot_user_id=BOT_ID,
        allowed_channel_ids=frozenset({CHANNEL_ID}),
    )

    assert request.kind is RoleRequestKind.RETIREMENT


def test_pending_codec_round_trip_supports_ftt_entry(role_request_env) -> None:
    service, roles = _ftt_entry_service(role_request_env)
    embed = build_pending_embed(
        requester=role_request_env.requester,
        add_role_ids=tuple(role.id for role in roles[:5]),
        remove_role_ids=(),
        reason="Ganton · <@&714>",
        ui=service.ui,
        created_at=datetime(2026, 9, 21, 12, tzinfo=UTC),
        message_id=852,
        kind=RoleRequestKind.FTT_ENTRY,
    )
    message = FakeMessage(message_id=852, channel=role_request_env.channel, embed=embed)
    message.components[0].children[1].disabled = True

    request = decode_pending_message(
        message,
        bot_user_id=BOT_ID,
        allowed_channel_ids=frozenset({CHANNEL_ID}),
    )

    assert request.kind is RoleRequestKind.FTT_ENTRY
    assert request.reason == "Ganton · <@&714>"
    assert message.embeds[0].title == FTT_ENTRY_PENDING_TITLE
    assert [field.name for field in message.embeds[0].fields[:2]] == [
        REQUEST_KIND_FIELD_TITLE,
        STATION_FIELD_TITLE,
    ]


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
    assert partial.comment_input.required is True
    assert partial.comment_input.min_length == 1
    assert rejection.title == ROLE_REQUEST_MODAL_COPY.reject_title
    assert rejection.reason_input.placeholder == (ROLE_REQUEST_MODAL_COPY.reject_reason_placeholder)


@pytest.mark.asyncio
async def test_retirement_uses_reduced_modal_and_is_excluded_from_normal_catalog(
    role_request_env,
) -> None:
    service, retired = _retirement_service(role_request_env)

    additions, removals = service.requestable_role_choices(role_request_env.requester)
    modal = RetirementRoleRequestModal(
        service,
        member=role_request_env.requester,
        channel=role_request_env.channel,
    )

    assert retired not in additions
    assert retired not in removals
    assert modal.title == ROLE_REQUEST_MODAL_COPY.retirement_title
    assert len(modal.children) == 2
    assert modal.confirm.default is False


@pytest.mark.asyncio
async def test_retirement_approval_removes_all_manageable_roles_including_uncatalogued_ones(
    role_request_env,
) -> None:
    service, retired = _retirement_service(role_request_env)
    creation = _interaction(role_request_env, SimpleNamespace(id=0), user=role_request_env.requester)

    await service.create_retirement_request(
        creation,
        member=role_request_env.requester,
        channel=role_request_env.channel,
        reason="Cierre de etapa",
    )

    message = next(iter(role_request_env.channel.messages.values()))
    request = decode_pending_message(
        message,
        bot_user_id=BOT_ID,
        allowed_channel_ids=frozenset({CHANNEL_ID}),
    )
    assert request.kind is RoleRequestKind.RETIREMENT
    assert request.add_role_ids == (retired.id,)
    assert set(request.remove_role_ids) == {
        role_request_env.removable.id,
        role_request_env.requester.top_role.id,
    }
    assert [child.custom_id for child in message.components[0].children] == [
        APPROVE_ALL_CUSTOM_ID,
        REJECT_CUSTOM_ID,
    ]

    # Another role assigned after the request must still be removed.
    role_request_env.requester.roles.append(role_request_env.add_two)
    decision = _interaction(role_request_env, message)
    await service.approve_all(decision)

    final_ids = {role.id for role in role_request_env.requester.roles}
    assert final_ids == {retired.id}
    assert len(role_request_env.requester.edit_calls) == 1
    approved = {field.name: field.value for field in message.embeds[0].fields}[APPROVED_CHANGES_TITLE]
    assert role_request_env.add_two.mention in approved


@pytest.mark.asyncio
async def test_guided_role_request_buttons_follow_operational_membership(role_request_env) -> None:
    service, retired = _retirement_service(role_request_env)
    ftt_service, ftt_roles = _ftt_entry_service(role_request_env)
    service.ftt_entry_options = ftt_service.ftt_entry_options
    service.operational_staff_role_ids = frozenset({role_request_env.requester.top_role.id, 712})

    def labels() -> list[str]:
        view = RoleRequestStartView(
            service,
            member=role_request_env.requester,
            channel=role_request_env.channel,
        )
        return [child.label for child in view.children]

    assert labels() == [
        ROLE_REQUEST_MODAL_COPY.normal_button,
        ROLE_REQUEST_MODAL_COPY.retirement_button,
    ]
    role_request_env.requester.roles.remove(role_request_env.requester.top_role)
    assert labels() == [
        ROLE_REQUEST_MODAL_COPY.normal_button,
        ROLE_REQUEST_MODAL_COPY.ftt_entry_button,
    ]
    role_request_env.requester.roles.append(ftt_roles[2])
    assert labels() == [
        ROLE_REQUEST_MODAL_COPY.normal_button,
        ROLE_REQUEST_MODAL_COPY.retirement_button,
    ]
    role_request_env.requester.roles.append(retired)
    assert labels() == [ROLE_REQUEST_MODAL_COPY.normal_button]


def test_guided_request_submission_rechecks_member_roles(role_request_env) -> None:
    retirement_service, retired = _retirement_service(role_request_env)
    ftt_service, ftt_roles = _ftt_entry_service(role_request_env)
    ftt_service.operational_staff_role_ids = frozenset({role_request_env.requester.top_role.id, 712})
    ftt_service.retired_role_id = retired.id

    with pytest.raises(RoleRequestValidationError, match="perteneces a la facción"):
        ftt_service.ftt_entry_changes(role_request_env.requester, "ganton")

    role_request_env.requester.roles.remove(role_request_env.requester.top_role)
    with pytest.raises(RoleRequestValidationError, match="miembros activos"):
        retirement_service.retirement_changes(role_request_env.requester)

    role_request_env.requester.roles.append(ftt_roles[2])
    with pytest.raises(RoleRequestValidationError, match="perteneces a la facción"):
        ftt_service.ftt_entry_changes(role_request_env.requester, "ganton")

    role_request_env.requester.roles.append(retired)
    with pytest.raises(RoleRequestValidationError, match="reingreso"):
        ftt_service.ftt_entry_changes(role_request_env.requester, "ganton")


@pytest.mark.asyncio
async def test_retirement_warns_reviewer_about_roles_bot_cannot_remove(role_request_env) -> None:
    service, retired = _retirement_service(role_request_env)
    managed = FakeRole(920, name="Integración", position=2, managed=True)
    higher = FakeRole(921, name="Externo", position=51)
    for role in (managed, higher):
        role.guild = role_request_env.guild
        role_request_env.guild.roles.append(role)
        role_request_env.guild._roles[role.id] = role
        role_request_env.requester.roles.append(role)

    creation = _interaction(role_request_env, SimpleNamespace(id=0), user=role_request_env.requester)
    await service.create_retirement_request(
        creation,
        member=role_request_env.requester,
        channel=role_request_env.channel,
        reason=None,
    )
    message = next(iter(role_request_env.channel.messages.values()))
    decision = _interaction(role_request_env, message)
    await service.approve_all(decision)

    assert {role.id for role in role_request_env.requester.roles} == {retired.id, managed.id, higher.id}
    warning = decision.followup.messages[-1]["embed"].description
    assert managed.mention in warning
    assert higher.mention in warning
    assert decision.followup.messages[-1]["ephemeral"] is True


@pytest.mark.asyncio
async def test_v2_retirement_removes_roles_outside_catalog(role_request_env) -> None:
    service, retired = _retirement_service(role_request_env)
    repository = FakeRoleRequestRepository()
    service.repository = repository  # type: ignore[assignment]
    review_channel = FakeChannel(210)
    review_channel.guild = role_request_env.guild
    role_request_env.guild._channels[review_channel.id] = review_channel
    role_request_env.guild.text_channels.append(review_channel)
    service.review_channel_ids = (review_channel.id,)
    creation = _interaction(role_request_env, SimpleNamespace(id=0), user=role_request_env.requester)

    await service.create_retirement_request(
        creation,
        member=role_request_env.requester,
        channel=role_request_env.channel,
        reason=None,
    )
    role_request_env.requester.roles.append(role_request_env.add_two)
    review_message = next(iter(review_channel.messages.values()))
    await service.approve_all(_interaction(role_request_env, review_message))

    assert repository.request is not None
    assert repository.request.status is StoredRoleRequestStatus.APPROVED
    assert {role.id for role in role_request_env.requester.roles} == {retired.id}
    assert len(role_request_env.requester.edit_calls) == 1


@pytest.mark.asyncio
async def test_retirement_approval_rechecks_current_membership(role_request_env) -> None:
    service, retired = _retirement_service(role_request_env)
    creation = _interaction(role_request_env, SimpleNamespace(id=0), user=role_request_env.requester)
    await service.create_retirement_request(
        creation,
        member=role_request_env.requester,
        channel=role_request_env.channel,
        reason=None,
    )
    message = next(iter(role_request_env.channel.messages.values()))
    role_request_env.requester.roles.remove(role_request_env.requester.top_role)

    await service.approve_all(_interaction(role_request_env, message))

    assert not role_request_env.requester.edit_calls
    assert retired not in role_request_env.requester.roles


@pytest.mark.asyncio
async def test_ftt_approval_rechecks_current_membership(role_request_env) -> None:
    service, roles = _ftt_entry_service(role_request_env)
    creation = _interaction(role_request_env, SimpleNamespace(id=0), user=role_request_env.requester)
    await service.create_ftt_entry_request(
        creation,
        member=role_request_env.requester,
        channel=role_request_env.channel,
        station_key="ganton",
    )
    message = next(iter(role_request_env.channel.messages.values()))
    role_request_env.requester.roles.append(roles[2])

    await service.approve_all(_interaction(role_request_env, message))

    assert not role_request_env.requester.edit_calls
    assert roles[0] not in role_request_env.requester.roles


@pytest.mark.asyncio
async def test_retirement_cannot_be_partially_approved(role_request_env) -> None:
    service, _retired = _retirement_service(role_request_env)
    creation = _interaction(role_request_env, SimpleNamespace(id=0), user=role_request_env.requester)
    await service.create_retirement_request(
        creation,
        member=role_request_env.requester,
        channel=role_request_env.channel,
        reason=None,
    )
    message = next(iter(role_request_env.channel.messages.values()))
    interaction = _interaction(role_request_env, message)

    await service.approve_partial(
        interaction,
        guild_id=GUILD_ID,
        channel_id=CHANNEL_ID,
        message_id=message.id,
        selected_add=(704,),
        selected_remove=(703,),
        comment=None,
    )

    assert not role_request_env.requester.edit_calls
    assert ROLE_REQUEST_MODAL_COPY.retirement_partial_blocked in (
        interaction.followup.messages[-1]["embed"].description
    )


@pytest.mark.asyncio
async def test_ftt_entry_requests_only_station_and_applies_complete_package(
    role_request_env,
) -> None:
    service, roles = _ftt_entry_service(role_request_env)
    role_request_env.requester.roles.append(roles[1])
    modal = FttEntryRoleRequestModal(
        service,
        member=role_request_env.requester,
        channel=role_request_env.channel,
    )

    assert modal.title == ROLE_REQUEST_MODAL_COPY.ftt_entry_title
    assert [option.value for option in modal.station_select.options] == [
        "ganton",
        "dillimore",
    ]
    station, additions = service.ftt_entry_changes(role_request_env.requester, "ganton")
    assert station == "Ganton · <@&714>"
    assert tuple(role.id for role in additions) == (710, 712, 713, 714)

    creation = _interaction(
        role_request_env,
        SimpleNamespace(id=0),
        user=role_request_env.requester,
    )
    await service.create_ftt_entry_request(
        creation,
        member=role_request_env.requester,
        channel=role_request_env.channel,
        station_key="ganton",
    )

    message = next(iter(role_request_env.channel.messages.values()))
    request = decode_pending_message(
        message,
        bot_user_id=BOT_ID,
        allowed_channel_ids=frozenset({CHANNEL_ID}),
    )
    assert request.kind is RoleRequestKind.FTT_ENTRY
    assert request.add_role_ids == (710, 712, 713, 714)
    assert [child.custom_id for child in message.components[0].children] == [
        APPROVE_ALL_CUSTOM_ID,
        REJECT_CUSTOM_ID,
    ]

    await service.approve_all(_interaction(role_request_env, message))

    final_ids = {role.id for role in role_request_env.requester.roles}
    assert {710, 711, 712, 713, 714} <= final_ids
    welcome_channel = role_request_env.guild.get_channel(WELCOME_CHANNEL_ID)
    assert welcome_channel is not None
    assert len(welcome_channel.send_calls) == 1
    welcome = welcome_channel.send_calls[0]
    assert welcome["content"] == f"<@{REQUESTER_ID}>"
    welcome_embed = welcome["embed"]
    assert welcome_embed.title == "🎓 ¡Bienvenido al Field Training Program!"
    assert welcome_embed.author.name == role_request_env.requester.display_name
    assert welcome_embed.author.icon_url == "https://cdn.example/avatar.png"
    assert welcome_embed.description == (
        f"¡Bienvenido, <@{REQUESTER_ID}>!\n\n"
        f"Lean los últimos mensajes del <#{INFORMATION_CHANNEL_ID}> y "
        "[este post](https://sheriff.sarp.es/viewtopic.php?t=37) "
        "que trae más información acerca del FTP.\n\n"
        "También se recomienda ampliamente leer el contenido de "
        f"<#{ANNOUNCEMENTS_CHANNEL_ID}> y <#{BIG_BROTHER_CHANNEL_ID}>.\n\n"
        f"Cualquier duda que tengas, pregunta en el <#{QUESTIONS_CHANNEL_ID}> "
        "que los FTOs y supervisores están acá para darte una mano."
    )
    assert not welcome_embed.fields


@pytest.mark.asyncio
async def test_ftt_entry_cannot_be_partially_approved(role_request_env) -> None:
    service, _roles = _ftt_entry_service(role_request_env)
    creation = _interaction(
        role_request_env,
        SimpleNamespace(id=0),
        user=role_request_env.requester,
    )
    await service.create_ftt_entry_request(
        creation,
        member=role_request_env.requester,
        channel=role_request_env.channel,
        station_key="dillimore",
    )
    message = next(iter(role_request_env.channel.messages.values()))
    interaction = _interaction(role_request_env, message)

    await service.approve_partial(
        interaction,
        guild_id=GUILD_ID,
        channel_id=CHANNEL_ID,
        message_id=message.id,
        selected_add=(710,),
        selected_remove=(),
        comment="Solo una parte",
    )

    assert not role_request_env.requester.edit_calls
    assert ROLE_REQUEST_MODAL_COPY.ftt_entry_partial_blocked in (
        interaction.followup.messages[-1]["embed"].description
    )


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

    assert interaction.response.deferred == [False]
    assert len(role_request_env.requester.edit_calls) == 1
    role_ids = {role.id for role in role_request_env.requester.roles}
    assert 701 in role_ids
    assert 703 not in role_ids
    assert all(child.disabled for child in message.components[0].children)
    assert message.embeds[0].title == role_request_env.service.ui.status_approved
    assert not interaction.followup.messages

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
    assert not interaction.followup.messages


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
    assert not interaction.followup.messages


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
async def test_request_command_offers_private_retirement_choice_when_configured(
    role_request_env,
) -> None:
    service, _retired = _retirement_service(role_request_env)
    responses: list[dict[str, object]] = []
    modals: list[object] = []

    async def respond(content=None, **kwargs) -> None:
        responses.append({"content": content, **kwargs})

    async def send_modal(modal) -> None:
        modals.append(modal)

    ctx = SimpleNamespace(
        guild=role_request_env.guild,
        author=role_request_env.requester,
        channel=role_request_env.channel,
        respond=respond,
        send_modal=send_modal,
    )
    cog = RoleRequestCog(service)

    await cog.request_roles.callback(cog, ctx)

    assert not modals
    assert responses[-1]["ephemeral"] is True
    assert isinstance(responses[-1]["view"], RoleRequestStartView)


@pytest.mark.asyncio
async def test_request_command_offers_guided_ftt_entry_when_configured(
    role_request_env,
) -> None:
    service, _roles = _ftt_entry_service(role_request_env)
    responses: list[dict[str, object]] = []

    async def respond(content=None, **kwargs) -> None:
        responses.append({"content": content, **kwargs})

    async def unexpected_modal(_modal) -> None:
        raise AssertionError("the guided start view must be shown first")

    ctx = SimpleNamespace(
        guild=role_request_env.guild,
        author=role_request_env.requester,
        channel=role_request_env.channel,
        respond=respond,
        send_modal=unexpected_modal,
    )
    cog = RoleRequestCog(service)

    await cog.request_roles.callback(cog, ctx)

    view = responses[-1]["view"]
    assert responses[-1]["ephemeral"] is True
    assert isinstance(view, RoleRequestStartView)
    assert [child.label for child in view.children] == [
        ROLE_REQUEST_MODAL_COPY.normal_button,
        ROLE_REQUEST_MODAL_COPY.ftt_entry_button,
    ]


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
    outcomes = [followup.messages for followup in (first.followup, second.followup)]
    assert sum(bool(messages) for messages in outcomes) == 1
    stale = next(messages[-1] for messages in outcomes if messages)
    assert stale["embed"].description == role_request_env.service.ui.stale_request


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


def _configure_shifts(env, service=None):
    service = service or env.service
    options = []
    roles = []
    for index, key in enumerate(("am", "pm", "em")):
        role = FakeRole(720 + index, name=f"{key.upper()} Shift", position=2)
        role.guild = env.guild
        env.guild.roles.append(role)
        env.guild._roles[role.id] = role
        roles.append(role)
        options.append(RoleRequestShiftOption(
            key=key, label=role.name, role_id=role.id, description=f"Horario {key} ARG",
        ))
    service.shift_options = tuple(options)
    service.shift_role_ids = frozenset(role.id for role in roles)
    service.allowed_role_order = (*service.allowed_role_order, *(role.id for role in roles))
    configured = service.settings.model_copy(update={
        "role_request_allowed_role_ids_csv": ",".join(str(role_id) for role_id in service.allowed_role_order),
    })
    service.allowed_roles = configured.role_request_allowed_role_allowlist
    return roles


def _enable_private_requests(env, service):
    repository = FakeRoleRequestRepository()
    service.repository = repository
    channel = FakeChannel(210)
    channel.guild = env.guild
    env.guild._channels[channel.id] = channel
    env.guild.text_channels.append(channel)
    service.review_channel_ids = (channel.id,)
    return repository, channel


async def _open_ftt_approval(env, *, private):
    service, _ = _ftt_entry_service(env)
    repository, review = _enable_private_requests(env, service) if private else (None, env.channel)
    await service.create_ftt_entry_request(
        _interaction(env, SimpleNamespace(id=0), user=env.requester),
        member=env.requester, channel=env.channel, station_key="ganton",
    )
    message = next(iter(review.messages.values()))
    interaction = _interaction(env, message)
    # The generic persistent view must inspect the real request, not its default NORMAL kind.
    view = RoleRequestDecisionView(service)
    await view.approve_all_button.callback(interaction)
    assert not interaction.response.deferred
    assert not env.requester.edit_calls
    assert not env.guild.get_channel(WELCOME_CHANNEL_ID).send_calls
    if repository:
        assert repository.request.status is StoredRoleRequestStatus.PENDING
    modal = interaction.response.modals[0]
    assert isinstance(modal, FttEntryApprovalModal)
    return service, repository, message, modal


def _select_instructor(modal, env, user_id):
    # Exercise Pycord's actual UserSelect resolution using a Discord-shaped modal payload.
    resolved = {"users": {str(user_id): {
        "id": str(user_id), "username": "Instructor", "discriminator": "0", "avatar": None,
    }}}
    modal.instructor_select.refresh_from_modal(
        SimpleNamespace(
            guild=env.guild, data={"resolved": resolved},
            _state=SimpleNamespace(member_cache_flags=SimpleNamespace(interaction=False)),
        ),
        {"values": [str(user_id)]},
    )


@pytest.mark.parametrize("private", (False, True))
@pytest.mark.parametrize("with_instructor", (False, True))
async def test_ftt_approval_modal_optional_instructor_and_welcome(
    role_request_env, private, with_instructor,
):
    env = role_request_env
    service, repository, message, modal = await _open_ftt_approval(env, private=private)
    serialized = modal.to_dict()
    picker = serialized["components"][0]["component"]
    assert picker["type"] == discord.ComponentType.user_select.value
    assert picker["min_values"] == 0 and picker["max_values"] == 1
    assert picker["required"] is False
    assert modal.title == ROLE_REQUEST_MODAL_COPY.ftt_entry_approval_title
    if with_instructor:
        _select_instructor(modal, env, REVIEWER_ID)
    else:
        _select_modal_values(modal.instructor_select, [])
        # Even an empty user select includes the interaction state in real modal submissions.
        modal.instructor_select._interaction = SimpleNamespace(
            guild=env.guild, data={},
            _state=SimpleNamespace(member_cache_flags=SimpleNamespace(interaction=False)),
        )
    submitted = _interaction(env, message)
    await modal.callback(submitted)
    assert submitted.response.deferred == [False]
    assert len(env.requester.edit_calls) == 1
    assert {710, 711, 712, 713, 714} <= {role.id for role in env.requester.roles}
    welcome = env.guild.get_channel(WELCOME_CHANNEL_ID).send_calls[0]
    description = welcome["embed"].description
    assert f"¡Bienvenido, <@{REQUESTER_ID}>!" in description
    assert ("Y tu instructor principal será" in description) is with_instructor
    if with_instructor:
        assert description.endswith(
            f"Y tu instructor principal será <@{REVIEWER_ID}> "
            "(más información [aquí](https://sheriff.sarp.es/viewtopic.php?t=37))."
        )
    assert welcome["allowed_mentions"].to_dict() == {"users": [REQUESTER_ID], "parse": []}
    if repository:
        assert repository.request.status is StoredRoleRequestStatus.APPROVED
    assert service.ftt_entry_options.welcome.channel_id == WELCOME_CHANNEL_ID


@pytest.mark.parametrize("private", (False, True))
@pytest.mark.parametrize("invalid", ("left", "bot"))
async def test_ftt_instructor_must_still_be_a_human_guild_member(role_request_env, private, invalid):
    env = role_request_env
    _service, repository, message, modal = await _open_ftt_approval(env, private=private)
    _select_instructor(modal, env, REVIEWER_ID if invalid == "bot" else 99999)
    env.reviewer.bot = invalid == "bot"
    submitted = _interaction(env, message)
    await modal.callback(submitted)
    assert not env.requester.edit_calls
    assert not env.guild.get_channel(WELCOME_CHANNEL_ID).send_calls
    assert ROLE_REQUEST_MODAL_COPY.ftt_entry_instructor_unavailable in (
        submitted.followup.messages[-1]["embed"].description
    )
    assert submitted.followup.messages[-1]["ephemeral"] is True
    if repository:
        assert repository.request.status is StoredRoleRequestStatus.PENDING
    # A failed selection never consumes the request: a new approval can omit the instructor.
    retry = FttEntryApprovalModal(modal.service, request=modal.request)
    await retry.callback(_interaction(env, message))
    assert len(env.requester.edit_calls) == 1


@pytest.mark.parametrize("private", (False, True))
@pytest.mark.parametrize("invalid", ("lost_role", "different_guild"))
async def test_ftt_approval_rechecks_authorization_after_modal(role_request_env, private, invalid):
    env = role_request_env
    _service, repository, message, modal = await _open_ftt_approval(env, private=private)
    submitted = _interaction(env, message)
    if invalid == "lost_role":
        env.reviewer.roles = [env.everyone]
    else:
        submitted.guild = SimpleNamespace(id=999)
    await modal.callback(submitted)
    assert not env.requester.edit_calls
    assert not env.guild.get_channel(WELCOME_CHANNEL_ID).send_calls
    assert submitted.followup.messages[-1]["ephemeral"] is True
    if repository:
        assert repository.request.status is StoredRoleRequestStatus.PENDING


@pytest.mark.parametrize("private", (False, True))
async def test_ftt_approval_button_does_not_open_modal_for_unauthorized_user(role_request_env, private):
    env = role_request_env
    service, _repository, message, _modal = await _open_ftt_approval(env, private=private)
    view = RoleRequestDecisionView(service)
    interaction = _interaction(env, message, user=env.requester)
    await view.approve_all_button.callback(interaction)
    assert not interaction.response.modals
    assert interaction.response.messages[-1]["ephemeral"] is True
    assert not env.requester.edit_calls


@pytest.mark.parametrize("private", (False, True))
@pytest.mark.parametrize("welcome", ("disabled", "forbidden"))
async def test_ftt_approval_does_not_depend_on_welcome_delivery(role_request_env, private, welcome):
    env = role_request_env
    service, repository, message, modal = await _open_ftt_approval(env, private=private)
    channel = env.guild.get_channel(WELCOME_CHANNEL_ID)
    if welcome == "disabled":
        service.ftt_entry_options = service.ftt_entry_options.model_copy(update={"welcome": None})
    else:
        channel.send = AsyncMock(side_effect=discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"), "Cannot publish",
        ))
    await modal.callback(_interaction(env, message))
    assert len(env.requester.edit_calls) == 1
    assert not channel.send_calls
    if repository:
        assert repository.request.status is StoredRoleRequestStatus.APPROVED


@pytest.mark.parametrize("private", (False, True))
async def test_duplicate_ftt_modals_only_approve_and_welcome_once(role_request_env, private):
    env = role_request_env
    _service, repository, message, modal = await _open_ftt_approval(env, private=private)
    first, second = _interaction(env, message), _interaction(env, message)
    await asyncio.gather(modal.callback(first), modal.callback(second))
    assert len(env.requester.edit_calls) == 1
    assert len(env.guild.get_channel(WELCOME_CHANNEL_ID).send_calls) == 1
    assert any(
        response["embed"].description == modal.service.ui.stale_request
        for interaction in (first, second) for response in interaction.followup.messages
    )
    if repository:
        assert repository.request.status is StoredRoleRequestStatus.APPROVED


@pytest.mark.parametrize("private", (False, True))
@pytest.mark.parametrize("kind", ("normal", "retirement"))
async def test_non_ftt_approve_button_keeps_immediate_approval(role_request_env, private, kind):
    env = role_request_env
    service, _ = _retirement_service(env)
    repository, review = _enable_private_requests(env, service) if private else (None, env.channel)
    if kind == "retirement":
        await service.create_retirement_request(
            _interaction(env, SimpleNamespace(id=0), user=env.requester),
            member=env.requester, channel=env.channel, reason="Retiro QA",
        )
    else:
        await service._publish_new_request(
            interaction_id=9001, member=env.requester, channel=env.channel,
            add_roles=(env.add_one,), remove_roles=(), reason=None,
        )
    message = next(iter(review.messages.values()))
    view = RoleRequestDecisionView(service)
    interaction = _interaction(env, message)
    await view.approve_all_button.callback(interaction)
    assert not interaction.response.modals
    assert interaction.response.deferred == [False]
    assert len(env.requester.edit_calls) == 1
    if repository:
        assert repository.request.status is StoredRoleRequestStatus.APPROVED


async def test_ftt_modal_cannot_approve_a_normal_request(role_request_env):
    env = role_request_env
    message = _pending_message(env)
    request, _ = await env.service.resolve_interaction_request(_interaction(env, message))
    modal = FttEntryApprovalModal(env.service, request=request)
    interaction = _interaction(env, message)
    await modal.callback(interaction)
    assert not env.requester.edit_calls
    assert interaction.followup.messages[-1]["embed"].description == env.service.ui.stale_request


def test_ftt_welcome_accepts_old_configuration_without_new_channels(role_request_env):
    options = RoleRequestFttWelcomeOptions(
        channel_id=WELCOME_CHANNEL_ID, rules_url="https://example.org/ftp",
        reference_channel_ids=(ANNOUNCEMENTS_CHANNEL_ID,),
    )
    embed = build_ftt_entry_welcome_embed(role_request_env.requester, options)
    assert "[este post](https://example.org/ftp)" in embed.description
    assert f"<#{ANNOUNCEMENTS_CHANNEL_ID}>" in embed.description
    assert "None" not in embed.description
    assert "Cualquier duda" not in embed.description
    assert "instructor" not in embed.description


async def _publish_replacement_test_request(env, interaction_id):
    return await env.service._publish_new_request(
        interaction_id=interaction_id, member=env.requester, channel=env.channel,
        add_roles=(env.add_one,), remove_roles=(), reason="Motivo privado QA",
    )


@pytest.mark.parametrize("missing", ("none", "public", "private", "all"))
async def test_replacement_updates_remaining_cards_and_disables_old_controls(role_request_env, missing):
    env = role_request_env
    repo, first = _enable_private_requests(env, env.service)
    second = FakeChannel(211)
    second.guild = env.guild
    env.guild._channels[second.id] = second
    env.service.review_channel_ids = (first.id, second.id)
    old_message, *_ = await _publish_replacement_test_request(env, 9100)
    old = repo.request
    old_mirrors = [next(iter(channel.messages.values())) for channel in (first, second)]
    # A missing old message must not reuse its snowflake in the fake send().
    if missing in ("public", "all"):
        env.channel.fetch_message = AsyncMock(side_effect=_not_found())
    if missing in ("private", "all"):
        first.fetch_message = AsyncMock(side_effect=_not_found())
    if missing == "all":
        second.fetch_message = AsyncMock(side_effect=_not_found())
    # DB-backed creation must never scan channel history to decide whether it can proceed.
    env.service.find_pending_request = AsyncMock(
        side_effect=AssertionError("Unexpected Discord history scan"),
    )
    new_message, *_ = await _publish_replacement_test_request(env, 9101)
    assert repo.history[old.id].status is StoredRoleRequestStatus.CANCELLED
    assert repo.request.status is StoredRoleRequestStatus.PENDING
    assert repo.cancellation_due[old.id] is None
    if missing not in ("public", "all"):
        assert old_message.embeds[0].title == "Solicitud cancelada"
        assert old_message.embeds[0].color == discord.Color.dark_grey()
        assert new_message.jump_url in old_message.embeds[0].description
        assert "Motivo privado QA" not in str(old_message.embeds[0].to_dict())
    for index, mirror in enumerate(old_mirrors):
        if missing == "all" or (missing == "private" and index == 0):
            assert not mirror.edit_calls
            continue
        assert mirror.embeds[0].title == "Solicitud cancelada"
        assert mirror.embeds[0].color == discord.Color.dark_grey()
        assert new_message.jump_url in mirror.embeds[0].description
        assert all(child.disabled for child in mirror.components[0].children)
        assert mirror.edit_calls[-1]["allowed_mentions"].to_dict() == {"parse": []}
    assert not env.requester.edit_calls and not env.requester.dm_calls
    # Even an old modal/click cannot approve the cancelled request.
    with pytest.raises(RoleRequestCodecError, match="no longer pending"):
        await env.service.resolve_interaction_request(_interaction(env, old_mirrors[0]))


async def test_replacement_card_failure_retries_without_removing_new_request(role_request_env, caplog):
    env = role_request_env
    repo, review = _enable_private_requests(env, env.service)
    old_message, *_ = await _publish_replacement_test_request(env, 9200)
    old = repo.request
    original_edit = old_message.edit
    old_message.edit = AsyncMock(side_effect=discord.Forbidden(
        SimpleNamespace(status=403, reason="Forbidden"), "Cannot edit",
    ))
    new_message, *_ = await _publish_replacement_test_request(env, 9201)
    assert repo.request.status is StoredRoleRequestStatus.PENDING
    assert not new_message.deleted
    assert repo.cancellation_due[old.id] is not None
    assert repo.cancellation_due[old.id] > datetime.now(UTC)
    assert "role_request_cancellation_card_failed" in caplog.text
    assert next(iter(review.messages.values())).embeds[0].title == "Solicitud cancelada"
    old_message.edit = original_edit
    # The scheduler retrieves persisted cancellations after restarting.
    registry = RoleRequestServiceRegistry(env.service.bot, None, repo)
    registry.get = AsyncMock(return_value=env.service)
    await registry.sync_cancellations()
    assert repo.cancellation_due[old.id] is None
    assert old_message.embeds[0].title == "Solicitud cancelada"


async def test_failed_replacement_publication_leaves_old_cards_and_pending_state(role_request_env):
    env = role_request_env
    repo, review = _enable_private_requests(env, env.service)
    old_message, *_ = await _publish_replacement_test_request(env, 9300)
    old = repo.request
    review.send = AsyncMock(side_effect=discord.Forbidden(
        SimpleNamespace(status=403, reason="Forbidden"), "Cannot publish",
    ))
    with pytest.raises(RoleRequestValidationError):
        await _publish_replacement_test_request(env, 9301)
    assert repo.history[old.id].status is StoredRoleRequestStatus.PENDING
    assert not old_message.edit_calls and not old_message.deleted
    assert len(env.channel.messages) == 1


async def test_invalid_replacement_does_not_reserve_or_cancel(role_request_env):
    env = role_request_env
    repo, _ = _enable_private_requests(env, env.service)
    await _publish_replacement_test_request(env, 9400)
    old = repo.request
    with pytest.raises(RoleRequestValidationError):
        await env.service._publish_new_request(
            interaction_id=9401, member=env.requester, channel=env.channel,
            add_roles=(), remove_roles=(), reason=None,
        )
    assert repo.request.id == old.id and repo.request.status is StoredRoleRequestStatus.PENDING
    assert not repo.history


@pytest.mark.asyncio
async def test_shift_forms_are_separate_required_for_entry_optional_for_changes(role_request_env):
    env = role_request_env
    service, _ = _ftt_entry_service(env)
    _configure_shifts(env, service)
    entry = FttEntryRoleRequestModal(service, member=env.requester, channel=env.channel)
    normal = CreateRoleRequestModal(service, member=env.requester, channel=env.channel)
    assert entry.shift_select.required and entry.shift_select.min_values == 1
    assert normal.shift_select.required is False and normal.shift_select.min_values == 0
    for modal in (entry, normal):
        assert modal.shift_select.max_values == 1
        assert [option.value for option in modal.shift_select.options] == ["am", "pm", "em"]
        assert all("ARG" in option.description for option in modal.shift_select.options)
        assert len(modal.children) <= 5
    general = {option.value for select in (*normal.add_selects, *normal.remove_selects)
               for option in select.options}
    assert not general.intersection({"720", "721", "722"})


@pytest.mark.asyncio
@pytest.mark.parametrize("private", (False, True))
@pytest.mark.parametrize("previous", ((), (720,), (720, 722)))
async def test_shift_entry_approves_package_and_replaces_previous(role_request_env, private, previous):
    env = role_request_env
    service, _ = _ftt_entry_service(env)
    _configure_shifts(env, service)
    env.requester.roles.extend(env.guild.get_role(role_id) for role_id in previous)
    repository, review = _enable_private_requests(env, service) if private else (None, env.channel)
    creation = _interaction(env, SimpleNamespace(id=0), user=env.requester)
    await service.create_ftt_entry_request(
        creation, member=env.requester, channel=env.channel, station_key="ganton", shift_key="pm",
    )
    message = next(iter(review.messages.values()))
    fields = {field.name: field.value for field in message.embeds[0].fields}
    assert fields[SHIFT_FIELD_TITLE].endswith("→ <@&721>")
    await service.approve_all(_interaction(env, message))
    ids = {role.id for role in env.requester.roles}
    assert {710, 711, 712, 713, 714, 721} <= ids
    assert ids & {720, 721, 722} == {721}
    if repository:
        assert repository.request.shift_change.target_role_id == 721
        assert repository.request.status is StoredRoleRequestStatus.APPROVED


@pytest.mark.asyncio
@pytest.mark.parametrize("key", (None, "wrong"))
async def test_entry_rejects_missing_or_unknown_shift(role_request_env, key):
    env = role_request_env
    service, _ = _ftt_entry_service(env)
    _configure_shifts(env, service)
    interaction = _interaction(env, SimpleNamespace(id=0), user=env.requester)
    await service.create_ftt_entry_request(
        interaction, member=env.requester, channel=env.channel, station_key="ganton", shift_key=key,
    )
    assert not env.channel.messages
    assert interaction.followup.messages[-1]["embed"].description == ROLE_REQUEST_MODAL_COPY.shift_required


@pytest.mark.asyncio
@pytest.mark.parametrize("private", (False, True))
async def test_shift_only_request_persists_and_approves_as_one_unit(role_request_env, private):
    env = role_request_env
    roles = _configure_shifts(env)
    env.requester.roles.append(roles[0])
    service = env.service
    repository, review = _enable_private_requests(env, service) if private else (None, env.channel)
    creation = _interaction(env, SimpleNamespace(id=0), user=env.requester)
    await service.create_request(
        creation, member=env.requester, channel=env.channel,
        add_roles=(), remove_roles=(), reason="Privado", shift_key="em",
    )
    message = next(iter(review.messages.values()))
    request, _ = await service.resolve_interaction_request(_interaction(env, message))
    assert request.shift_change == RoleRequestShiftChange(target_role_id=722, previous_role_ids=(720,))
    partial = PartialApprovalModal(service, request=request, guild=env.guild)
    assert partial.add_select is None and partial.remove_select is None
    assert partial.shift_select is not None
    invalid = _interaction(env, message)
    await service.approve_partial(
        invalid, guild_id=GUILD_ID, channel_id=review.id, message_id=message.id,
        selected_add=(722,), selected_remove=(), comment="Incompleto",
    )
    assert not env.requester.edit_calls
    assert invalid.followup.messages[-1]["embed"].description == ROLE_REQUEST_MODAL_COPY.shift_partial_blocked
    if repository:
        assert repository.request.status is StoredRoleRequestStatus.PENDING
    await service.approve_all(_interaction(env, message))
    assert {role.id for role in env.requester.roles} & {720, 721, 722} == {722}
    assert len(env.requester.edit_calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("shift_key", (None, "am"))
async def test_unchanged_shift_does_not_change_it_or_require_a_selection(role_request_env, shift_key):
    env = role_request_env
    roles = _configure_shifts(env)
    env.requester.roles.append(roles[0])
    creation = _interaction(env, SimpleNamespace(id=0), user=env.requester)
    await env.service.create_request(
        creation, member=env.requester, channel=env.channel, add_roles=(env.add_one,),
        remove_roles=(), reason=None, shift_key=shift_key,
    )
    message = next(iter(env.channel.messages.values()))
    assert SHIFT_FIELD_TITLE not in {field.name for field in message.embeds[0].fields}
    await env.service.approve_all(_interaction(env, message))
    assert {role.id for role in env.requester.roles} & {720, 721, 722} == {720}


@pytest.mark.asyncio
@pytest.mark.parametrize("private", (False, True))
async def test_shift_approval_rejects_new_conflicting_shift_since_submission(role_request_env, private):
    env = role_request_env
    roles = _configure_shifts(env)
    env.requester.roles.append(roles[0])
    repository, review = _enable_private_requests(env, env.service) if private else (None, env.channel)
    await env.service.create_request(
        _interaction(env, SimpleNamespace(id=0), user=env.requester), member=env.requester,
        channel=env.channel, add_roles=(), remove_roles=(), reason=None, shift_key="pm",
    )
    env.requester.roles.append(roles[2])
    message = next(iter(review.messages.values()))
    approval = _interaction(env, message)
    await env.service.approve_all(approval)
    assert not env.requester.edit_calls
    assert approval.followup.messages[-1]["embed"].description == ROLE_REQUEST_MODAL_COPY.shift_stale
    if repository:
        assert repository.request.status is StoredRoleRequestStatus.PENDING


def test_shift_options_reject_duplicates_and_other_role_overlap():
    option = {"key": "am", "label": "AM", "role_id": 720, "description": "AM ARG"}
    with pytest.raises(ValueError, match="unique"):
        RoleRequestOptions(shifts=(option, option))
    with pytest.raises(ValueError, match="distinct"):
        RoleRequestOptions(retired_role_id=720, shifts=(option,))


@pytest.mark.asyncio
@pytest.mark.parametrize("approve_shift", (False, True))
async def test_partial_modal_callback_groups_shift_and_preserves_other_roles(role_request_env, approve_shift):
    env = role_request_env
    roles = _configure_shifts(env)
    env.requester.roles.append(roles[0])
    repository, review = _enable_private_requests(env, env.service)
    normal = CreateRoleRequestModal(env.service, member=env.requester, channel=env.channel)
    for select in (*normal.add_selects, *normal.remove_selects):
        _select_modal_values(select, [])
    _select_modal_values(normal.add_selects[0], ["701", "702"])
    _select_modal_values(normal.shift_select, ["pm"])
    await normal.callback(_interaction(env, SimpleNamespace(id=0), user=env.requester))
    message = next(iter(review.messages.values()))
    request, _ = await env.service.resolve_interaction_request(_interaction(env, message))
    partial = PartialApprovalModal(env.service, request=request, guild=env.guild)
    _select_modal_values(partial.add_select, ["701"])
    _select_modal_values(partial.shift_select, ["approve"] if approve_shift else [])
    partial.comment_input.refresh_state({"value": "Solo estos cambios"})
    await partial.callback(_interaction(env, message))
    assert repository.request.status is StoredRoleRequestStatus.PARTIALLY_APPROVED
    ids = {role.id for role in env.requester.roles}
    assert 701 in ids and 702 not in ids
    assert ids & {720, 721, 722} == ({721} if approve_shift else {720})
    assert 703 in ids  # An unrelated existing role must survive.


@pytest.mark.asyncio
async def test_entry_modal_callback_and_info_include_shifts(role_request_env):
    env = role_request_env
    service, _ = _ftt_entry_service(env)
    _configure_shifts(env, service)
    modal = FttEntryRoleRequestModal(service, member=env.requester, channel=env.channel)
    _select_modal_values(modal.station_select, ["dillimore"])
    _select_modal_values(modal.shift_select, ["am"])
    await modal.callback(_interaction(env, SimpleNamespace(id=0), user=env.requester))
    message = next(iter(env.channel.messages.values()))
    assert "<@&720>" in str(message.embeds[0].to_dict())
    responses = []

    async def respond(**kwargs):
        responses.append(kwargs)

    ctx = SimpleNamespace(guild=env.guild, author=env.requester, channel=env.channel, respond=respond)
    cog = RoleRequestCog(service)
    await cog.info_roles.callback(cog, ctx)
    rendered = str(responses[-1]["embed"].to_dict())
    assert all(text in rendered for text in ("AM Shift", "PM Shift", "EM Shift", "ARG", "reemplazará"))
    assert responses[-1]["ephemeral"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ("protected", "missing", "foreign"))
async def test_shift_selection_rejects_unmanageable_unknown_and_foreign_roles(role_request_env, fault):
    env = role_request_env
    roles = _configure_shifts(env)
    key = "am"
    if fault == "protected":
        roles[0].position = 100
    elif fault == "missing":
        del env.guild._roles[720]
    else:
        key = "other_guild_shift"
    creation = _interaction(env, SimpleNamespace(id=0), user=env.requester)
    await env.service.create_request(
        creation, member=env.requester, channel=env.channel, add_roles=(), remove_roles=(),
        reason=None, shift_key=key,
    )
    assert not env.channel.send_calls and not env.requester.edit_calls
    assert creation.followup.messages[-1]["ephemeral"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("private", (False, True))
@pytest.mark.parametrize("decision", ("approve", "partial", "reject"))
async def test_role_requests_and_decisions_work_without_attachments(
    role_request_env, private, decision,
):
    env = role_request_env
    service = env.service
    review = _enable_private_requests(env, service)[1] if private else env.channel
    await service.create_request(
        _interaction(env, SimpleNamespace(id=0), user=env.requester), member=env.requester,
        channel=env.channel, add_roles=(env.add_one, env.add_two), remove_roles=(), reason=None,
    )
    channels = [env.channel, review] if private else [env.channel]
    for channel in channels:
        sent = channel.send_calls[0]
        assert not sent["embed"].thumbnail
        assert "file" not in sent
    message = next(iter(review.messages.values()))
    interaction = _interaction(env, message)
    if decision == "approve":
        await service.approve_all(interaction)
    elif decision == "partial":
        await service.approve_partial(
            interaction, guild_id=env.guild.id, channel_id=review.id, message_id=message.id,
            selected_add=(env.add_one.id,), selected_remove=(), comment="Solo uno",
        )
    else:
        await service.reject(
            interaction, guild_id=env.guild.id, channel_id=review.id, message_id=message.id,
            reason="No corresponde",
        )
    for channel in channels:
        card = next(iter(channel.messages.values()))
        assert not card.embeds[0].thumbnail
        assert not card.attachments
        assert all("file" not in edit for edit in card.edit_calls)
        assert card.embeds[0].color != discord.Color.gold()
    if private:
        assert "file" not in env.requester.dm_calls[-1]
        assert not env.requester.dm_calls[-1]["embed"].thumbnail


@pytest.mark.asyncio
async def test_legacy_request_keeps_existing_attachments_on_resolution(role_request_env):
    env = role_request_env
    message = _pending_message(env)
    message.embeds[0].remove_thumbnail()
    other = SimpleNamespace(filename="existing.png")
    message.attachments = [other]
    await env.service.approve_all(_interaction(env, message))
    assert not message.embeds[0].thumbnail
    assert "file" not in message.edit_calls[-1]
    assert message.attachments[0] is other
    assert len(message.attachments) == 1


@pytest.mark.asyncio
async def test_role_command_opens_without_attachment_permission(role_request_env, monkeypatch):
    env = role_request_env
    permissions = FakePermissions()
    permissions.attach_files = False
    monkeypatch.setattr(env.channel, "permissions_for", lambda _: permissions)
    env.service.validate_channel_permissions(env.guild, env.channel)
    env.service.review_channel_ids = (env.channel.id,)
    assert env.service.resolve_review_channels(env.guild) == (env.channel,)
    responses, modals = [], []

    async def respond(**kwargs):
        responses.append(kwargs)

    async def send_modal(modal):
        modals.append(modal)

    ctx = SimpleNamespace(
        guild=env.guild, author=env.requester, channel=env.channel, respond=respond, send_modal=send_modal,
    )
    cog = RoleRequestCog(env.service)
    await cog.request_roles.callback(cog, ctx)
    assert modals or any(isinstance(item.get("view"), RoleRequestStartView) for item in responses)
    assert not any(item["embed"].description == env.service.ui.subsystem_unavailable for item in responses)

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from pydantic import ValidationError

from arrest_report_agent.blacklists import (
    BlacklistCog,
    BlacklistDeleteModal,
    BlacklistEditModal,
    BlacklistEntry,
    BlacklistEntryStatus,
    BlacklistLookupModal,
    BlacklistRegistrationModal,
    build_entry_embed,
    build_info_embed,
    character_name_key,
    parse_lookup,
    parse_pcu_profile_url,
    parse_reference,
)
from arrest_report_agent.blacklists import discord as blacklist_discord
from arrest_report_agent.config import BlacklistOptions, BlacklistTypeOptions
from arrest_report_agent.infrastructure.discord.forms import ResultVisibility

GUILD_ID = 1234674521416663070
CCW_CHANNEL_ID = 1530621801871441991
OPERATIONAL_ROLE_ID = 111
MANAGER_ROLE_ID = 222


def _options() -> BlacklistOptions:
    return BlacklistOptions(
        manager_role_ids=(MANAGER_ROLE_ID,),
        types=(
            BlacklistTypeOptions(
                key="ccw",
                display_name="CCW",
                reference_prefix="CCW",
                channel_ids=(CCW_CHANNEL_ID,),
            ),
        )
    )


def _entry() -> BlacklistEntry:
    timestamp = datetime(2026, 9, 12, 12, 30, tzinfo=UTC)
    return BlacklistEntry(
        id=UUID("00000000-0000-0000-0000-000000000001"),
        guild_id=GUILD_ID,
        type_key="ccw",
        type_name="CCW",
        reference_prefix="CCW",
        number=1,
        channel_id=CCW_CHANNEL_ID,
        message_id=999,
        interaction_id=1000,
        author_user_id=42,
        pcu_profile_id=542,
        pcu_url="https://ucp.sarp.es/app/ucp/characters/overview/542",
        character_name="Peter Kinsella",
        character_name_key="peter kinsella",
        reason=(
            "Realizó un acto ilegal; intento de caza y portación de una escopeta sin licencia."
        ),
        status=BlacklistEntryStatus.ACTIVE,
        created_at=timestamp,
        updated_at=timestamp,
    )


class FakeProvider:
    def __init__(self, options: BlacklistOptions | None) -> None:
        self.options = options

    async def cached(self, _guild_id: int) -> SimpleNamespace:
        return SimpleNamespace(
            settings=SimpleNamespace(
                blacklist_options=self.options,
                operational_staff_role_ids=(OPERATIONAL_ROLE_ID,),
            )
        )


class FakeContext:
    def __init__(self, channel_id: int, *role_ids: int) -> None:
        self.guild = SimpleNamespace(id=GUILD_ID)
        self.channel = SimpleNamespace(id=channel_id)
        resolved_roles = role_ids or (OPERATIONAL_ROLE_ID,)
        self.author = SimpleNamespace(
            id=42,
            roles=tuple(SimpleNamespace(id=role_id) for role_id in resolved_roles),
        )
        self.interaction = SimpleNamespace(id=888)
        self.modals: list[object] = []
        self.responses: list[dict[str, object]] = []

    async def send_modal(self, modal: object) -> None:
        self.modals.append(modal)

    async def respond(self, **kwargs: object) -> None:
        self.responses.append(kwargs)


class FakeLookupRepository:
    def __init__(self, entries: tuple[BlacklistEntry, ...]) -> None:
        self.entries = entries
        self.calls: list[dict[str, object]] = []

    async def search(self, **kwargs: object) -> tuple[BlacklistEntry, ...]:
        self.calls.append(kwargs)
        return self.entries


class FakeMutationRepository(FakeLookupRepository):
    def __init__(self, entry: BlacklistEntry) -> None:
        super().__init__((entry,))
        self.entry = entry
        self.update_calls: list[dict[str, object]] = []
        self.revoke_calls: list[dict[str, object]] = []

    async def get_active_by_reference(self, **kwargs: object) -> BlacklistEntry | None:
        self.calls.append(kwargs)
        return self.entry

    async def update_active(self, **kwargs: object) -> BlacklistEntry:
        self.update_calls.append(kwargs)
        timestamp = datetime(2026, 9, 12, 13, 0, tzinfo=UTC)
        return self.entry.model_copy(
            update={
                "pcu_url": kwargs["raw_pcu_url"],
                "character_name": kwargs["raw_character_name"],
                "reason": kwargs["raw_reason"],
                "edited_by_user_id": kwargs["actor_user_id"],
                "edited_at": timestamp,
                "updated_at": timestamp,
            }
        )

    async def revoke_active(self, **kwargs: object) -> BlacklistEntry:
        self.revoke_calls.append(kwargs)
        timestamp = datetime(2026, 9, 12, 13, 0, tzinfo=UTC)
        return self.entry.model_copy(
            update={
                "status": BlacklistEntryStatus.REVOKED,
                "revoked_by_user_id": kwargs["actor_user_id"],
                "revoked_at": timestamp,
                "updated_at": timestamp,
            }
        )


class FakeRegistrationRepository(FakeLookupRepository):
    def __init__(self) -> None:
        super().__init__(())
        self.publishing = _entry().model_copy(
            update={"message_id": None, "status": BlacklistEntryStatus.PUBLISHING}
        )
        self.reserve = AsyncMock(return_value=(self.publishing, True))
        self.mark_active = AsyncMock(return_value=_entry())
        self.mark_failed = AsyncMock()


def test_pcu_profile_parser_canonicalizes_the_supported_character_url() -> None:
    profile_id, canonical = parse_pcu_profile_url(
        " https://UCP.SARP.ES/app/ucp/characters/overview/542/ "
    )

    assert profile_id == 542
    assert canonical == "https://ucp.sarp.es/app/ucp/characters/overview/542"
    assert parse_lookup("542") == (542, None)
    assert parse_lookup(canonical) == (542, None)


@pytest.mark.parametrize(
    "url",
    (
        "http://ucp.sarp.es/app/ucp/characters/overview/542",
        "https://example.com/app/ucp/characters/overview/542",
        "https://ucp.sarp.es/app/ucp/characters/overview/542?x=1",
        "https://ucp.sarp.es/app/ucp/characters/542",
    ),
)
def test_pcu_profile_parser_rejects_urls_outside_the_exact_profile_route(url: str) -> None:
    with pytest.raises(ValueError):
        parse_pcu_profile_url(url)


def test_character_search_key_is_case_accent_and_punctuation_insensitive() -> None:
    assert character_name_key("  PÉTER_Kinsella  ") == "peter kinsella"
    assert parse_lookup("Péter-Kinsella") == (None, "peter kinsella")


def test_blacklist_reference_parser_requires_a_full_typed_reference() -> None:
    assert parse_reference(" ccw-00001 ") == ("CCW", 1)
    with pytest.raises(ValueError, match="CCW-00001"):
        parse_reference("1")


def test_blacklist_registry_maps_channels_and_rejects_ambiguous_ownership() -> None:
    options = _options()

    assert options.type_for_channel(CCW_CHANNEL_ID) == options.types[0]
    assert options.type_for_channel(999) is None

    with pytest.raises(ValidationError, match="only one type"):
        BlacklistOptions(
            types=(
                options.types[0],
                BlacklistTypeOptions(
                    key="internal_affairs",
                    display_name="Internal Affairs",
                    reference_prefix="IA",
                    channel_ids=(CCW_CHANNEL_ID,),
                ),
            )
        )

    with pytest.raises(ValidationError, match="manager role IDs must be unique"):
        BlacklistOptions(
            types=options.types,
            manager_role_ids=(MANAGER_ROLE_ID, MANAGER_ROLE_ID),
        )


def test_entry_embed_contains_the_type_reference_and_structured_fields() -> None:
    embed = build_entry_embed(_entry())

    assert embed.title == "🚫 CCW-00001 · Blacklist de CCW"
    assert [field.name for field in embed.fields] == [
        "Cuenta de PCU",
        "Personaje",
        "Razón de la blacklist",
        "Estado",
        "Registrado por",
    ]
    assert "ID 542" in embed.fields[0].value
    assert embed.fields[1].value == "Peter Kinsella"
    assert embed.fields[3].value == "🔴 Activa"
    assert embed.fields[4].value == "<@42>"


@pytest.mark.asyncio
async def test_blacklist_modals_have_the_requested_fields_and_public_default() -> None:
    cog = BlacklistCog(FakeProvider(_options()), FakeLookupRepository(()))  # type: ignore[arg-type]
    registration = BlacklistRegistrationModal(
        cog,
        requester_id=42,
        guild_id=GUILD_ID,
        channel_id=CCW_CHANNEL_ID,
        type_key="ccw",
        type_name="CCW",
    )
    lookup = BlacklistLookupModal(cog, requester_id=42)
    edit = BlacklistEditModal(
        cog,
        requester_id=42,
        guild_id=GUILD_ID,
        channel_id=CCW_CHANNEL_ID,
        type_key="ccw",
        type_name="CCW",
    )
    delete = BlacklistDeleteModal(
        cog,
        requester_id=42,
        guild_id=GUILD_ID,
        channel_id=CCW_CHANNEL_ID,
        type_key="ccw",
        type_name="CCW",
    )

    assert registration.title == "Registrar blacklist · CCW"
    assert [child.label for child in registration.children] == [
        "URL de la cuenta en PCU",
        "Personaje con el que la solicitó",
        "Razón de la blacklist",
    ]
    assert registration.reason.min_length == 10
    assert registration.reason.max_length == 1_000
    assert lookup.title == "Consultar blacklists"
    assert [option.label for option in lookup.visibility.options] == [
        "Visible para todos",
        "Solo para mí",
    ]
    assert lookup.visibility.options[0].default is True
    assert registration.pcu_url.placeholder.endswith("/1234")
    assert [child.label for child in edit.children] == [
        "Referencia",
        "Nueva URL de la cuenta en PCU",
        "Nuevo personaje",
        "Nueva razón",
    ]
    assert edit.pcu_url.required is False
    assert edit.character_name.required is False
    assert edit.reason.required is False
    assert [option.value for option in delete.confirmation.options] == ["confirm"]


@pytest.mark.asyncio
async def test_registration_command_only_opens_in_a_configured_blacklist_channel() -> None:
    cog = BlacklistCog(FakeProvider(_options()), FakeLookupRepository(()))  # type: ignore[arg-type]
    allowed = FakeContext(CCW_CHANNEL_ID)
    refused = FakeContext(999)

    await BlacklistCog.register.callback(cog, allowed)  # type: ignore[arg-type]
    await BlacklistCog.register.callback(cog, refused)  # type: ignore[arg-type]

    assert isinstance(allowed.modals[0], BlacklistRegistrationModal)
    assert allowed.modals[0].type_key == "ccw"
    assert refused.modals == []
    assert refused.responses[0]["ephemeral"] is True
    assert "no está asociado" in refused.responses[0]["embed"].description


@pytest.mark.asyncio
async def test_lookup_command_opens_from_any_guild_channel() -> None:
    cog = BlacklistCog(FakeProvider(_options()), FakeLookupRepository(()))  # type: ignore[arg-type]
    context = FakeContext(987654321)

    await BlacklistCog.lookup.callback(cog, context)  # type: ignore[arg-type]

    assert isinstance(context.modals[0], BlacklistLookupModal)
    assert context.responses == []


@pytest.mark.asyncio
async def test_operational_role_is_required_for_registration_and_lookup() -> None:
    cog = BlacklistCog(FakeProvider(_options()), FakeLookupRepository(()))  # type: ignore[arg-type]
    registration = FakeContext(CCW_CHANNEL_ID, 999)
    lookup = FakeContext(987654321, 999)

    await BlacklistCog.register.callback(cog, registration)  # type: ignore[arg-type]
    await BlacklistCog.lookup.callback(cog, lookup)  # type: ignore[arg-type]

    assert registration.modals == []
    assert lookup.modals == []
    assert "personal operativo" in registration.responses[0]["embed"].description
    assert "personal operativo" in lookup.responses[0]["embed"].description


@pytest.mark.asyncio
async def test_edit_and_delete_require_manager_roles_and_the_configured_channel() -> None:
    cog = BlacklistCog(FakeProvider(_options()), FakeLookupRepository(()))  # type: ignore[arg-type]
    operative = FakeContext(CCW_CHANNEL_ID)
    manager_edit = FakeContext(CCW_CHANNEL_ID, OPERATIONAL_ROLE_ID, MANAGER_ROLE_ID)
    manager_delete = FakeContext(CCW_CHANNEL_ID, OPERATIONAL_ROLE_ID, MANAGER_ROLE_ID)

    await BlacklistCog.edit.callback(cog, operative)  # type: ignore[arg-type]
    await BlacklistCog.edit.callback(cog, manager_edit)  # type: ignore[arg-type]
    await BlacklistCog.delete.callback(cog, manager_delete)  # type: ignore[arg-type]

    assert operative.modals == []
    assert "Executive Staff" in operative.responses[0]["embed"].description
    assert isinstance(manager_edit.modals[0], BlacklistEditModal)
    assert isinstance(manager_delete.modals[0], BlacklistDeleteModal)


@pytest.mark.asyncio
async def test_info_is_public_and_does_not_require_an_operational_role() -> None:
    cog = BlacklistCog(FakeProvider(_options()), FakeLookupRepository(()))  # type: ignore[arg-type]
    context = FakeContext(987654321, 999)

    await BlacklistCog.info.callback(cog, context)  # type: ignore[arg-type]

    assert context.responses[0]["ephemeral"] is False
    assert context.responses[0]["embed"].title == "🚫 Sistema de blacklists"
    assert "/blacklist editar" in str(context.responses[0]["embed"].to_dict())


def test_info_embed_lists_enabled_types_without_exposing_a_type_selector() -> None:
    embed = build_info_embed(_options())

    assert "CCW" in str(embed.to_dict())
    assert "Ningún usuario puede elegir" in (embed.description or "")


@pytest.mark.parametrize(
    ("visibility", "expected_ephemeral"),
    (
        (ResultVisibility.PUBLIC, False),
        (ResultVisibility.PRIVATE, True),
    ),
)
@pytest.mark.asyncio
async def test_lookup_honors_visibility_and_searches_without_a_type_filter(
    visibility: ResultVisibility,
    expected_ephemeral: bool,
) -> None:
    repository = FakeLookupRepository((_entry(),))
    cog = BlacklistCog(FakeProvider(_options()), repository)  # type: ignore[arg-type]
    responses: list[dict[str, object]] = []

    async def send_message(**kwargs: object) -> None:
        responses.append(kwargs)

    interaction = SimpleNamespace(
        guild=SimpleNamespace(id=GUILD_ID),
        channel=SimpleNamespace(id=999999),
        user=SimpleNamespace(
            id=42,
            roles=(SimpleNamespace(id=OPERATIONAL_ROLE_ID),),
        ),
        response=SimpleNamespace(send_message=send_message, is_done=lambda: False),
        followup=SimpleNamespace(),
        id=321,
    )

    await cog.publish_lookup(
        interaction,
        requester_id=42,
        raw_query="542",
        visibility=visibility,
    )

    assert repository.calls == [
        {"guild_id": GUILD_ID, "raw_query": "542", "limit": 10}
    ]
    assert "type_key" not in repository.calls[0]
    assert responses[0]["ephemeral"] is expected_ephemeral
    assert responses[0]["embed"].title == "🚫 CCW-00001 · Blacklist de CCW"
    assert responses[0]["allowed_mentions"].everyone is False


def _manager_interaction() -> SimpleNamespace:
    return SimpleNamespace(
        guild=SimpleNamespace(id=GUILD_ID),
        channel=SimpleNamespace(id=CCW_CHANNEL_ID),
        user=SimpleNamespace(
            id=42,
            roles=(
                SimpleNamespace(id=OPERATIONAL_ROLE_ID),
                SimpleNamespace(id=MANAGER_ROLE_ID),
            ),
        ),
        response=SimpleNamespace(defer=AsyncMock(), is_done=lambda: True),
        followup=SimpleNamespace(send=AsyncMock()),
        delete_original_response=AsyncMock(),
        id=654,
    )


@pytest.mark.asyncio
async def test_registration_publishes_without_an_ephemeral_confirmation() -> None:
    repository = FakeRegistrationRepository()
    cog = BlacklistCog(FakeProvider(_options()), repository)  # type: ignore[arg-type]
    channel = SimpleNamespace(id=CCW_CHANNEL_ID, send=AsyncMock(return_value=SimpleNamespace(id=999)))
    interaction = SimpleNamespace(
        guild=SimpleNamespace(id=GUILD_ID),
        channel=channel,
        user=SimpleNamespace(
            id=42,
            roles=(SimpleNamespace(id=OPERATIONAL_ROLE_ID),),
        ),
        response=SimpleNamespace(defer=AsyncMock(), is_done=lambda: True),
        followup=SimpleNamespace(send=AsyncMock()),
        delete_original_response=AsyncMock(),
        id=1000,
    )

    await cog.publish_registration(
        interaction,
        requester_id=42,
        guild_id=GUILD_ID,
        channel_id=CCW_CHANNEL_ID,
        type_key="ccw",
        raw_pcu_url="https://ucp.sarp.es/app/ucp/characters/overview/542",
        raw_character_name="Peter Kinsella",
        raw_reason="Razón válida para esta entrada de blacklist.",
    )

    channel.send.assert_awaited_once()
    interaction.delete_original_response.assert_awaited_once_with()
    interaction.followup.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_edit_updates_postgres_and_the_original_embed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = FakeMutationRepository(_entry())
    cog = BlacklistCog(FakeProvider(_options()), repository)  # type: ignore[arg-type]
    interaction = _manager_interaction()
    message = SimpleNamespace(edit=AsyncMock())
    monkeypatch.setattr(
        blacklist_discord,
        "_fetch_entry_message",
        AsyncMock(return_value=message),
    )

    await cog.publish_edit(
        interaction,
        requester_id=42,
        guild_id=GUILD_ID,
        channel_id=CCW_CHANNEL_ID,
        type_key="ccw",
        raw_reference="CCW-00001",
        raw_pcu_url="",
        raw_character_name="",
        raw_reason="Razón corregida para esta entrada de blacklist.",
    )

    assert repository.update_calls[0]["raw_pcu_url"] == _entry().pcu_url
    assert repository.update_calls[0]["raw_character_name"] == "Peter Kinsella"
    assert repository.update_calls[0]["actor_user_id"] == 42
    edited_embed = message.edit.await_args.kwargs["embed"]
    assert edited_embed.title == "🚫 CCW-00001 · Blacklist de CCW"
    assert any(field.name == "Última edición" for field in edited_embed.fields)
    interaction.delete_original_response.assert_awaited_once_with()
    interaction.followup.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_delete_removes_the_message_and_preserves_a_revoked_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = FakeMutationRepository(_entry())
    cog = BlacklistCog(FakeProvider(_options()), repository)  # type: ignore[arg-type]
    interaction = _manager_interaction()
    message = SimpleNamespace(delete=AsyncMock())
    monkeypatch.setattr(
        blacklist_discord,
        "_fetch_entry_message",
        AsyncMock(return_value=message),
    )

    await cog.publish_delete(
        interaction,
        requester_id=42,
        guild_id=GUILD_ID,
        channel_id=CCW_CHANNEL_ID,
        type_key="ccw",
        raw_reference="CCW-00001",
    )

    message.delete.assert_awaited_once_with()
    assert repository.revoke_calls[0]["actor_user_id"] == 42
    assert repository.revoke_calls[0]["type_key"] == "ccw"
    interaction.delete_original_response.assert_awaited_once_with()
    interaction.followup.send.assert_not_awaited()

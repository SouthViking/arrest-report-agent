from __future__ import annotations

import asyncio
import io
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import discord
import pytest
from PIL import Image
from pydantic import ValidationError

from arrest_report_agent.config import (
    AccessPolicyKey,
    AccessPolicyMode,
    AccessPolicySnapshot,
    GuildAgentConfig,
    GuildConfigSnapshot,
    GuildFeatureSnapshot,
    GuildRuntimeSettings,
    StolenWeaponOptions,
)
from arrest_report_agent.config.database import POLICY_SUBJECT_TYPES
from arrest_report_agent.infrastructure.assets import UploadedAsset
from arrest_report_agent.infrastructure.discord.cog import ReviewCog
from arrest_report_agent.infrastructure.discord.config_admin import FEATURE_LABELS, FeatureConfigurationModal
from arrest_report_agent.infrastructure.discord.stolen_weapons import (
    RegistrationContext,
    StolenWeaponCog,
    TheftDetailsModal,
    TheftPublicationView,
    build_theft_embeds,
)
from arrest_report_agent.infrastructure.storage import MemoryObjectStorage, ObjectStorageError
from arrest_report_agent.stolen_weapons.models import StolenWeaponReport, TheftDetails, normalize_serial
from arrest_report_agent.stolen_weapons.service import StolenWeaponService, validate_images

GUILD = 1234674521416663070
SOURCE = 1421923642983317545
DESTINATION = 1565184787591860345
USER = 123456789012345678
NOW = datetime(2026, 10, 28, 18, tzinfo=UTC)


def _details(**overrides):
    return TheftDetails.parse(
        **{
            "serial": "  sa-001234  ",
            "description": "El arma fue sustraída desde el vehículo.",
            "stolen_on": "27/10/2026",
            "victim_name": "Peter Kinsella",
            "reporting_officer_name": "Andrew Sánchez",
            **overrides,
        }
    )


def _image(format="PNG", color="red"):
    output = io.BytesIO()
    Image.new("RGB", (8, 8), color=color).save(output, format=format)
    return output.getvalue()


def _file(format="PNG", color="red"):
    return UploadedAsset("foto.png", "image/png", _image(format, color))


def _setup():
    member = MagicMock(spec=discord.Member)
    member.id, member.roles, member.bot = USER, [SimpleNamespace(id=50)], False
    member.display_name = "Andrew Sánchez"
    guild = SimpleNamespace(id=GUILD, me=SimpleNamespace(id=999), filesize_limit=10_000_000)
    source, destination = MagicMock(spec=discord.TextChannel), MagicMock(spec=discord.TextChannel)
    source.id, destination.id = SOURCE, DESTINATION
    source.guild = destination.guild = guild
    source.permissions_for.return_value = SimpleNamespace(view_channel=True)
    destination.permissions_for.return_value = SimpleNamespace(
        view_channel=True,
        send_messages=True,
        embed_links=True,
        attach_files=True,
    )
    guild.get_channel = lambda id: destination if id == DESTINATION else None
    guild.fetch_member = AsyncMock(return_value=member)
    destination.send = AsyncMock(return_value=SimpleNamespace(id=555))
    options = StolenWeaponOptions(source_channel_ids=(SOURCE,), destination_channel_id=DESTINATION)
    settings = SimpleNamespace(
        stolen_weapon_options=options,
        operational_staff_role_ids=(50,),
        max_file_bytes=1_000_000,
        max_image_bytes=1_000_000,
        max_total_file_bytes=5_000_000,
    )
    provider = SimpleNamespace(cached=AsyncMock(return_value=SimpleNamespace(settings=settings)))

    async def create(**values):
        return StolenWeaponReport(
            id=uuid4(),
            submission_id=values["submission_id"],
            guild_id=values["guild_id"],
            source_channel_id=values["source_channel_id"],
            destination_channel_id=values["destination_channel_id"],
            registered_by=values["registered_by"],
            details=values["details"],
            created_at=NOW,
            publication_status="pending",
            message_id=None,
            assets=values["assets"],
        )

    repository = SimpleNamespace(
        create=AsyncMock(side_effect=create),
        claim_publication=AsyncMock(return_value=True),
        mark_published=AsyncMock(),
        mark_failed=AsyncMock(),
    )
    storage = MemoryObjectStorage()
    service = StolenWeaponService(repository, storage)
    cog = StolenWeaponCog(provider, service)
    context = RegistrationContext(USER, GUILD, SOURCE, DESTINATION)
    return SimpleNamespace(
        member=member,
        guild=guild,
        source=source,
        destination=destination,
        settings=settings,
        repository=repository,
        storage=storage,
        service=service,
        cog=cog,
        context=context,
    )


def _interaction(state, id=100):
    response_state = {"done": False}

    async def defer(**_kwargs):
        response_state["done"] = True

    message = SimpleNamespace(delete=AsyncMock(), edit=AsyncMock())
    return SimpleNamespace(
        id=id,
        guild=state.guild,
        channel=state.source,
        user=state.member,
        response=SimpleNamespace(
            defer=AsyncMock(side_effect=defer),
            send_message=AsyncMock(),
            send_modal=AsyncMock(),
            is_done=lambda: response_state["done"],
        ),
        followup=SimpleNamespace(send=AsyncMock()),
        edit_original_response=AsyncMock(return_value=message),
        delete_original_response=AsyncMock(),
    )


def _publication(state):
    view = TheftPublicationView(state.cog, state.context, _details(), submission_id=101)
    view.files = (_file(),)
    return view


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("  sa-00012  ", "SA-00012"),
        ("00012", "00012"),
        ("a / b", "A / B"),
        ("SA_00012", "SA_00012"),
        ("ｓａ-００１２", "SA-0012"),
    ],
)
def test_serial_normalization_preserves_leading_zeroes_and_separators(raw, expected):
    assert normalize_serial(raw) == expected
    assert normalize_serial("SA-0012") != normalize_serial("SA0012")


@pytest.mark.parametrize(
    "overrides, message",
    [
        ({"serial": " \t "}, "serial"),
        ({"serial": "<@123>"}, "serial"),
        ({"serial": "a" * 101}, "serial"),
        ({"description": "   "}, "hechos"),
        ({"description": "a" * 2001}, "hechos"),
        ({"stolen_on": "31/02/2026"}, "fecha"),
        ({"stolen_on": "2026-10-28"}, "fecha"),
        ({"stolen_on": "1/1/2026"}, "fecha"),
        ({"victim_name": " \t "}, "propietario"),
        ({"reporting_officer_name": "  "}, "ayudante"),
    ],
)
def test_text_validation_rejects_invalid_or_blank_values(overrides, message):
    with pytest.raises(ValueError, match=message):
        _details(**overrides)


def test_theft_date_is_a_calendar_day_separate_from_registration_time():
    details = _details()
    assert details.stolen_on == date(2026, 10, 27)
    assert details.serial == "sa-001234"
    assert details.normalized_serial == "SA-001234"


@pytest.mark.parametrize("format", ["PNG", "JPEG", "WEBP", "GIF"])
def test_image_validation_uses_bytes_and_assigns_unique_attachment_names(format):
    files = validate_images((_file(format), _file(format)), max_file_bytes=100_000, max_total_bytes=500_000)
    assert files[0].filename != files[1].filename
    assert files[0].content_type.startswith("image/")


@pytest.mark.parametrize(
    "files, file_limit, total_limit",
    [
        ((), 10000, 10000),
        ((_file(),) * 6, 10000, 100000),
        ((UploadedAsset("fake.png", "image/png", b"not an image"),), 10000, 10000),
        ((_file(),), 1, 10000),
        ((_file(), _file()), 10000, 1),
        ((UploadedAsset("empty.png", "image/png", b""),), 10000, 10000),
    ],
)
def test_invalid_images_or_limits_are_rejected_before_storage(files, file_limit, total_limit):
    with pytest.raises(ValueError):
        validate_images(files, max_file_bytes=file_limit, max_total_bytes=total_limit)


def test_config_channels_must_be_explicit_positive_and_unique():
    for payload in (
        {"source_channel_ids": []},
        {"source_channel_ids": [1, 1]},
        {"source_channel_ids": [-1]},
        {"destination_channel_id": 0},
    ):
        with pytest.raises(ValidationError):
            StolenWeaponOptions.model_validate(
                {
                    "source_channel_ids": [SOURCE],
                    "destination_channel_id": DESTINATION,
                    **payload,
                }
            )


def test_configuration_requires_staff_policy_and_runtime_respects_disabled_state(settings):
    agent = GuildAgentConfig(
        model=settings.openai_model,
        reasoning_effort=settings.openai_reasoning_effort,
        **{
            field: getattr(settings, field)
            for field in GuildAgentConfig.model_fields
            if field not in {"model", "reasoning_effort"}
        },
    )
    values = dict(
        guild_id=GUILD,
        revision_id=uuid4(),
        revision=1,
        agent=agent,
        policies={
            key: AccessPolicySnapshot(
                key=key,
                subject_type=POLICY_SUBJECT_TYPES[key],
                mode=AccessPolicyMode.DISABLED,
            )
            for key in AccessPolicyKey
        },
        features={},
        persona_revision_id=uuid4(),
        persona_id="roy_velazco",
        persona_revision=1,
    )
    assert GuildRuntimeSettings(settings, GuildConfigSnapshot(**values)).stolen_weapon_options is None
    feature = GuildFeatureSnapshot(
        key="stolen_weapons",
        enabled=True,
        schema_version=1,
        payload={"source_channel_ids": [SOURCE], "destination_channel_id": DESTINATION},
    )
    values["features"] = {"stolen_weapons": feature}
    with pytest.raises(ValueError, match="restricted operational staff"):
        GuildConfigSnapshot(**values)
    values["policies"][AccessPolicyKey.OPERATIONAL_STAFF_ROLES] = AccessPolicySnapshot(
        key=AccessPolicyKey.OPERATIONAL_STAFF_ROLES,
        subject_type="role",
        mode=AccessPolicyMode.RESTRICTED,
        ids=(50,),
    )
    runtime = GuildRuntimeSettings(settings, GuildConfigSnapshot(**values))
    assert runtime.stolen_weapon_options.destination_channel_id == DESTINATION
    values["features"]["stolen_weapons"] = feature.model_copy(update={"enabled": False})
    assert GuildRuntimeSettings(settings, GuildConfigSnapshot(**values)).stolen_weapon_options is None
    values["features"]["stolen_weapons"] = feature.model_copy(update={"schema_version": 2})
    with pytest.raises(ValueError, match="schema version 1"):
        GuildConfigSnapshot(**values)


async def test_configuration_selector_exposes_module_within_discord_limits():
    modal = FeatureConfigurationModal(SimpleNamespace(), USER)
    assert "stolen_weapons" in {option.value for option in modal.key.options}
    assert len(modal.key.options) <= 25
    assert "stolen_weapons" in FEATURE_LABELS


@pytest.mark.parametrize(
    "enabled,staff,channel,visible",
    [
        (True, True, SOURCE, True),
        (False, True, SOURCE, False),
        (True, False, SOURCE, False),
        (True, True, 888, False),
    ],
)
async def test_help_shows_registration_only_where_authorized(
    settings, persona, enabled, staff, channel, visible
):
    state = _setup()
    state.member.roles = [SimpleNamespace(id=50)] if staff else []
    state.settings.stolen_weapon_options = state.settings.stolen_weapon_options if enabled else None
    state.settings.rumor_options = None
    provider = SimpleNamespace(
        cached=AsyncMock(
            return_value=SimpleNamespace(
                snapshot=SimpleNamespace(features={}),
                settings=state.settings,
            )
        )
    )
    bot = SimpleNamespace(
        user=SimpleNamespace(
            id=999,
            display_avatar=SimpleNamespace(url="https://cdn.discordapp.com/bot.png"),
        )
    )
    cog = ReviewCog(bot, settings, SimpleNamespace(), persona, config_provider=provider)
    ctx = SimpleNamespace(
        guild=state.guild, channel=SimpleNamespace(id=channel), author=state.member, respond=AsyncMock()
    )
    await ReviewCog.review_help.callback(cog, ctx)
    assert ("/registrar-robo-arma" in str(ctx.respond.await_args.kwargs["embed"].to_dict())) is visible
    shown = str(ctx.respond.await_args.kwargs["embed"].to_dict())
    assert ("/info-robos-armas" in shown) is (enabled and staff)


def _registration_modal(state, count=1):
    modal = TheftDetailsModal(state.cog, state.context)
    for field, value in (
        (modal.serial, "SA-001234"),
        (modal.description, "Sustraída desde el vehículo."),
        (modal.stolen_on, "27/10/2026"),
        (modal.victim, "Peter Kinsella"),
    ):
        field.refresh_state({"value": value})
    modal.upload._attachments = [SimpleNamespace(
        filename="foto.png", content_type="image/png", size=len(_image()),
        read=AsyncMock(return_value=_image()),
    ) for _ in range(count)]
    return modal


async def test_command_without_arguments_opens_one_modal_with_text_and_images():
    state = _setup()
    ctx = SimpleNamespace(
        guild=state.guild, channel=state.source, author=state.member,
        send_modal=AsyncMock(), respond=AsyncMock(),
    )
    await StolenWeaponCog.register_theft.callback(state.cog, ctx)
    modal = ctx.send_modal.await_args.args[0]
    assert isinstance(modal, TheftDetailsModal)
    bot = discord.Bot(intents=discord.Intents.none())
    bot.add_cog(state.cog)
    command = next(c for c in bot.pending_application_commands if c.name == "registrar-robo-arma")
    command.integration_types = {discord.IntegrationType.guild_install}
    assert command.to_dict().get("options", []) == []
    assert len(modal.to_dict()["components"]) == 5
    assert modal.description.max_length == 2000
    assert all(item.required for item in (modal.serial, modal.description, modal.stolen_on, modal.victim))
    assert modal.upload.required and modal.upload.min_values == 1 and modal.upload.max_values == 5
    assert not hasattr(modal, "officer")
    state.repository.create.assert_not_awaited()
    assert state.storage.objects == {}


async def test_missing_evidence_shows_only_private_error_without_creating_record():
    state = _setup()
    modal = _registration_modal(state, count=0)
    interaction = _interaction(state)
    await modal.callback(interaction)
    assert "1 y 5" in interaction.edit_original_response.await_args.kwargs["content"]
    state.repository.create.assert_not_awaited()
    state.destination.send.assert_not_awaited()
    assert not state.storage.objects


@pytest.mark.parametrize("count", [1, 5])
async def test_modal_submission_directly_publishes_and_captures_actual_actor_without_success_message(count):
    state = _setup()
    modal = _registration_modal(state, count=count)
    interaction = _interaction(state)
    await modal.callback(interaction)
    assert state.repository.create.await_count == 1
    assert state.destination.send.await_count == 1
    saved = state.repository.create.await_args.kwargs
    assert saved["registered_by"] == USER
    assert saved["details"].reporting_officer_name == f"Andrew Sánchez ({USER})"
    interaction.edit_original_response.assert_not_awaited()
    interaction.followup.send.assert_not_awaited()
    interaction.response.send_message.assert_not_awaited()
    interaction.delete_original_response.assert_awaited_once()
    assert modal.publication.closed
    # Duplicate deliveries of a modal submission reuse the original registration.
    await modal.callback(_interaction(state, 101))
    state.repository.create.assert_awaited_once()
    state.destination.send.assert_awaited_once()


async def test_role_revoked_while_downloading_evidence_prevents_registration():
    state = _setup()
    modal = _registration_modal(state)
    revoked = MagicMock(spec=discord.Member)
    revoked.id, revoked.bot, revoked.roles = USER, False, []
    state.guild.fetch_member.side_effect = [state.member, revoked]
    interaction = _interaction(state)
    await modal.callback(interaction)
    state.repository.create.assert_not_awaited()
    state.destination.send.assert_not_awaited()
    assert "rol habilitado" in interaction.edit_original_response.await_args.kwargs["content"]


async def test_concurrent_modal_submissions_register_and_publish_once():
    state = _setup()
    modal = _registration_modal(state)
    await asyncio.gather(modal.callback(_interaction(state, 101)), modal.callback(_interaction(state, 102)))
    state.repository.create.assert_awaited_once()
    state.destination.send.assert_awaited_once()


@pytest.mark.parametrize("invalid", ["channel", "role", "bot", "dm", "disabled", "permissions"])
async def test_command_rejects_unauthorized_usage_including_administrator_without_staff_role(invalid):
    state = _setup()
    ctx = SimpleNamespace(
        guild=state.guild,
        channel=state.source,
        author=state.member,
        send_modal=AsyncMock(),
        respond=AsyncMock(),
    )
    if invalid == "channel":
        state.source.id = 999
    elif invalid == "role":
        state.member.roles = [SimpleNamespace(id=777)]
        state.member.guild_permissions = SimpleNamespace(administrator=True)
    elif invalid == "bot":
        state.member.bot = True
    elif invalid == "dm":
        ctx.guild = None
    elif invalid == "disabled":
        state.settings.stolen_weapon_options = None
    else:
        state.destination.permissions_for.return_value.attach_files = False
    await StolenWeaponCog.register_theft.callback(state.cog, ctx)
    ctx.send_modal.assert_not_awaited()
    assert ctx.respond.await_args.kwargs["ephemeral"]


@pytest.mark.parametrize("invalid", ["bad_bytes", "too_big", "wrong_user", "invalid_text", "lost_role"])
async def test_direct_form_rejects_invalid_evidence_or_changed_identity_before_persisting(invalid):
    state = _setup()
    modal = _registration_modal(state)
    interaction = _interaction(state)
    if invalid == "bad_bytes":
        modal.upload._attachments[0].read.return_value = b"not an image"
    elif invalid == "too_big":
        modal.upload._attachments[0].size = 2_000_000
    elif invalid == "wrong_user":
        interaction.user = SimpleNamespace(id=999)
    elif invalid == "invalid_text":
        modal.stolen_on.refresh_state({"value": "not-a-date"})
    else:
        state.member.roles = []
    await modal.callback(interaction)
    state.repository.create.assert_not_awaited()
    state.destination.send.assert_not_awaited()
    assert not state.storage.objects
    assert interaction.edit_original_response.await_args.kwargs["content"]


@pytest.mark.parametrize("change", ["role", "source", "guild", "user", "destination", "disabled"])
async def test_publication_rechecks_context_and_current_config(change):
    state = _setup()
    view, interaction = _publication(state), _interaction(state)
    if change == "role":
        state.member.roles = []
    elif change == "source":
        state.source.id = 9
    elif change == "guild":
        state.guild.id = 9
    elif change == "user":
        state.member.id = 9
    elif change == "destination":
        state.settings.stolen_weapon_options = StolenWeaponOptions(
            source_channel_ids=(SOURCE,),
            destination_channel_id=888,
        )
    else:
        state.settings.stolen_weapon_options = None
    await view.publish(interaction)
    state.repository.create.assert_not_awaited()
    state.destination.send.assert_not_awaited()
    assert interaction.edit_original_response.await_args.kwargs["content"]


@pytest.mark.parametrize("count", [1, 5])
async def test_publication_persists_bucket_and_database_before_publishing_without_success_message(count):
    state = _setup()
    view, interaction = _publication(state), _interaction(state)
    view.files = tuple(_file(color="red" if i % 2 else "blue") for i in range(count))
    events = []
    original_create = state.repository.create.side_effect

    async def create(**values):
        assert all(asset.object_key in state.storage.objects for asset in values["assets"])
        events.append("database")
        return await original_create(**values)

    async def publish(**values):
        events.append("discord")
        assert len(values["files"]) == count
        assert values["allowed_mentions"].users is False
        assert values["allowed_mentions"].roles is False
        return SimpleNamespace(id=555)

    state.repository.create.side_effect = create
    state.destination.send.side_effect = publish
    await view.publish(interaction)
    assert events == ["database", "discord"]
    report = view.report
    assert report.registered_by == USER
    assert report.details.reporting_officer_name == "Andrew Sánchez"
    assert len(report.assets) == count
    for asset in report.assets:
        assert await state.storage.get(asset.object_key) in {_image(color="red"), _image(color="blue")}
        assert asset.object_key.startswith(f"stolen-weapons/{GUILD}/")
    state.repository.mark_published.assert_awaited_once_with(GUILD, report.id, 555)
    interaction.delete_original_response.assert_awaited_once()
    assert view.closed and not view.files


async def test_concurrent_submissions_do_not_register_or_publish_twice():
    state = _setup()
    view = _publication(state)
    await asyncio.gather(
        view.publish(_interaction(state, 111)),
        view.publish(_interaction(state, 112)),
    )
    state.repository.create.assert_awaited_once()
    state.destination.send.assert_awaited_once()


async def test_bucket_write_failure_does_not_create_record_or_publish():
    state = _setup()
    state.storage.put = AsyncMock(side_effect=ObjectStorageError("unavailable"))
    view, interaction = _publication(state), _interaction(state)
    await view.publish(interaction)
    state.repository.create.assert_not_awaited()
    state.destination.send.assert_not_awaited()
    assert not view.closed


async def test_database_failure_does_not_publish_and_uploads_are_reused_on_retry():
    state = _setup()
    state.storage.put = AsyncMock(wraps=state.storage.put)
    create = state.repository.create.side_effect
    state.repository.create.side_effect = RuntimeError("database offline")
    view = _publication(state)
    await view.publish(_interaction(state))
    state.destination.send.assert_not_awaited()
    state.repository.create.side_effect = create
    await view.retry_button.callback(_interaction(state, 102))
    state.destination.send.assert_awaited_once()
    state.storage.put.assert_awaited_once()


async def test_bucket_read_failure_can_retry_without_losing_record_or_claiming_publication():
    state = _setup()
    get = state.storage.get
    state.storage.get = AsyncMock(side_effect=ObjectStorageError("temporary"))
    view = _publication(state)
    await view.publish(_interaction(state))
    state.repository.claim_publication.assert_not_awaited()
    state.storage.get = get
    await view.retry_button.callback(_interaction(state, 102))
    state.repository.create.assert_awaited_once()
    state.destination.send.assert_awaited_once()


async def test_definitive_discord_permission_failure_can_retry_same_saved_record():
    state = _setup()
    state.destination.send.side_effect = discord.Forbidden(
        SimpleNamespace(status=403, reason="Forbidden"), {}
    )
    view = _publication(state)
    await view.publish(_interaction(state))
    state.repository.mark_failed.assert_awaited_once_with(GUILD, view.report.id)
    assert not view.closed
    state.destination.send.side_effect = None
    await view.retry_button.callback(_interaction(state, 102))
    state.repository.create.assert_awaited_once()
    state.repository.mark_published.assert_awaited_once()


async def test_unknown_delivery_failure_preserves_record_and_prevents_duplicate_publication():
    state = _setup()
    state.destination.send.side_effect = TimeoutError("delivery uncertain")
    view, interaction = _publication(state), _interaction(state)
    await view.publish(interaction)
    assert view.closed and view.report.assets
    state.repository.mark_failed.assert_not_awaited()
    assert "están guardados" in interaction.edit_original_response.await_args.kwargs["content"]
    await view.retry_button.callback(_interaction(state, 102))
    state.destination.send.assert_awaited_once()


async def test_message_link_failure_does_not_delete_evidence_or_republish():
    state = _setup()
    state.repository.mark_published.side_effect = RuntimeError("db link failed")
    view, interaction = _publication(state), _interaction(state)
    await view.publish(interaction)
    assert view.closed
    assert "se publicó" in interaction.edit_original_response.await_args.kwargs["content"]
    state.repository.mark_failed.assert_not_awaited()
    assert state.storage.objects


async def test_retry_timeout_clears_temporary_files_without_registering_or_publishing():
    state = _setup()
    view = _publication(state)
    await view.on_timeout()
    assert view.closed and not view.files
    state.repository.create.assert_not_awaited()
    state.destination.send.assert_not_awaited()


def test_embed_has_all_data_and_images_without_mentions_or_field_overflow():
    details = _details(description="*" * 2000, victim_name="<@123456789012345678>")
    files = validate_images((_file(),) * 5, max_file_bytes=10000, max_total_bytes=100000)
    embeds = build_theft_embeds(
        details, registered_by=USER, files=files, reference="ARM-EXAMPLE", created_at=NOW
    )
    assert len(embeds) == 5
    data = embeds[0].to_dict()
    values = {field["name"]: field["value"] for field in data["fields"]}
    assert values["Fecha del robo"] == "27/10/2026"
    assert "Ayudante que reporta" not in values
    assert "<@123456789012345678>" not in values["Propietario / víctima"]
    assert values["Registrado por"] == f"<@{USER}>"
    assert all(len(field["value"]) <= 1024 for field in data["fields"])
    assert sum(len(embed) for embed in embeds) <= 6000
    assert [embed.image.url for embed in embeds] == [f"attachment://{file.filename}" for file in files]


@pytest.mark.parametrize("channel_id", [SOURCE, 888])
async def test_weapon_theft_help_publishes_for_operational_staff_in_any_channel(channel_id):
    state = _setup()
    ctx = SimpleNamespace(
        guild=state.guild, author=state.member, channel=SimpleNamespace(id=channel_id), respond=AsyncMock(),
    )
    await StolenWeaponCog.theft_info.callback(state.cog, ctx)
    sent = ctx.respond.await_args.kwargs
    assert sent["ephemeral"] is False
    assert sent["allowed_mentions"].users is False and sent["allowed_mentions"].roles is False
    text = str(sent["embed"].to_dict())
    assert "/registrar-robo-arma" in text and "/cerrar-reporte-robo" in text and "/ver-evidencia" in text
    assert str(SOURCE) in text and str(DESTINATION) in text
    assert "automáticamente" in text
    state.repository.create.assert_not_awaited()
    state.destination.send.assert_not_awaited()
    assert not state.storage.objects


@pytest.mark.parametrize("case", ["no_role", "bot", "dm", "disabled", "config_error"])
async def test_weapon_theft_help_rejects_unauthorized_or_unavailable_requests_privately(case):
    state = _setup()
    ctx = SimpleNamespace(guild=state.guild, author=state.member, respond=AsyncMock())
    if case == "no_role":
        state.member.roles = []
        state.member.guild_permissions = SimpleNamespace(administrator=True)
    elif case == "bot":
        state.member.bot = True
    elif case == "dm":
        ctx.guild = None
    elif case == "disabled":
        state.settings.stolen_weapon_options = None
    else:
        state.cog.provider.cached.side_effect = RuntimeError("private-configuration-error")
    await StolenWeaponCog.theft_info.callback(state.cog, ctx)
    assert ctx.respond.await_args.kwargs["ephemeral"] is True
    assert "embed" not in ctx.respond.await_args.kwargs
    assert "private-configuration-error" not in ctx.respond.await_args.args[0]

from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from arrest_report_agent.config import (
    AccessPolicyKey,
    AccessPolicyMode,
    AccessPolicySnapshot,
    GuildAgentConfig,
    GuildConfigSnapshot,
    GuildFeatureSnapshot,
    GuildRuntimeSettings,
)
from arrest_report_agent.config.database import POLICY_SUBJECT_TYPES
from arrest_report_agent.infrastructure.storage import MemoryObjectStorage
from arrest_report_agent.logbook import (
    CATEGORY_SUBTYPES,
    LogbookAsset,
    LogbookCategory,
    LogbookCategoryCount,
    LogbookEntry,
    LogbookFile,
    LogbookFileLimitError,
    LogbookService,
    LogbookStatus,
    LogbookSubtypeCount,
    LogbookSummary,
    LogbookSummaryModal,
    LogbookSummarySetupView,
    LogbookSummaryVisibility,
    MoneyDirection,
    available_subtypes,
    build_entry_embed,
    build_entry_gallery_view,
    build_guide_embed,
    build_summary_embed,
    parse_money_amount,
)
from arrest_report_agent.logbook.discord import LogbookCog, LogbookEntryModal, LogbookGuideModal
from arrest_report_agent.logbook.repository import LogbookRepository


class FakeRepository:
    def __init__(self) -> None:
        self.assets = ()

    async def replace_assets(self, entry_id, assets):
        now = datetime.now(UTC)
        self.assets = tuple(
            LogbookAsset(
                id=uuid.uuid4(),
                entry_id=entry_id,
                position=position,
                created_at=now,
                **asset.model_dump(),
            )
            for position, asset in enumerate(assets)
        )
        return self.assets


def _member(user_id: int, role_ids: tuple[int, ...]) -> discord.Member:
    roles = {role_id: SimpleNamespace(id=role_id) for role_id in role_ids}
    guild = SimpleNamespace(id=123, get_role=roles.get, default_role=None)
    state = SimpleNamespace(store_user=lambda data: SimpleNamespace(id=int(data["id"])))
    return discord.Member(
        data={"user": {"id": str(user_id)}, "roles": [str(role_id) for role_id in role_ids]},
        guild=guild,
        state=state,
    )


def _entry(*, number: int = 100_000) -> LogbookEntry:
    now = datetime.now(UTC)
    return LogbookEntry(
        id=uuid.uuid4(),
        guild_id=123,
        number=number,
        channel_id=456,
        interaction_id=789,
        author_user_id=321,
        category=LogbookCategory.FLEET,
        subtype="purchase",
        subject="Sultan 2015 · Concesionario",
        description="Compra de un vehículo para la flota departamental.",
        money_direction=MoneyDirection.WITHDRAWAL,
        money_amount=Decimal("150000.00"),
        money_detail="Sultan 2015 · Concesionario",
        status=LogbookStatus.PUBLISHING,
        version=1,
        occurred_at=now,
        created_at=now,
        updated_at=now,
    )


def _summary() -> LogbookSummary:
    return LogbookSummary(
        guild_id=123,
        date_from=date(2026, 8, 1),
        date_to=date(2026, 8, 24),
        total_entries=10,
        attachment_count=4,
        income_total=Decimal("500000.00"),
        withdrawal_total=Decimal("650000.00"),
        categories=(
            LogbookCategoryCount(category=LogbookCategory.FINANCE, count=7),
            LogbookCategoryCount(category=LogbookCategory.PERSONNEL, count=3),
        ),
        subtypes=(
            LogbookSubtypeCount(
                category=LogbookCategory.FINANCE,
                subtype="deposit",
                count=5,
            ),
            LogbookSubtypeCount(
                category=LogbookCategory.PERSONNEL,
                subtype="entry",
                count=3,
            ),
            LogbookSubtypeCount(
                category=LogbookCategory.FINANCE,
                subtype="withdrawal",
                count=2,
            ),
        ),
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("150000", Decimal("150000.00")),
        ("$150.000", Decimal("150000.00")),
        ("150000,50", Decimal("150000.50")),
        ("1,234.56", Decimal("1234.56")),
    ],
)
def test_money_parser_accepts_common_local_formats(raw: str, expected: Decimal) -> None:
    assert parse_money_amount(raw) == expected


@pytest.mark.parametrize("raw", ["", "cien", "-1", "0"])
def test_money_parser_rejects_invalid_values(raw: str) -> None:
    with pytest.raises(ValueError):
        parse_money_amount(raw)


def test_reference_padding_expands_without_a_five_digit_ceiling() -> None:
    entry = _entry()

    assert entry.reference == "BIT-100000"
    embed = build_entry_embed(entry, attachment_count=2)
    assert embed.title == "🚓 BIT-100000 · Compra"
    assert "$150.000,00" in embed.fields[2].value
    assert "<@321>" in embed.fields[-1].value


@pytest.mark.asyncio
async def test_multiple_logbook_images_use_native_gallery_and_keep_documents_downloadable() -> None:
    view = build_entry_gallery_view(
        _entry(),
        files=(
            LogbookFile("frontal.png", "image/png", b"front"),
            LogbookFile("informe.pdf", "application/pdf", b"report"),
            LogbookFile("lateral.jpg", "image/jpeg", b"side"),
        ),
        output_filenames=("frontal.png", "informe.pdf", "lateral.jpg"),
        attachment_count=3,
    )

    assert view.timeout is None
    assert len(view.children) == 2
    container = view.children[0]
    assert isinstance(container, discord.ui.Container)
    assert container.colour.value == 0x5865F2
    gallery = next(item for item in container.items if isinstance(item, discord.ui.MediaGallery))
    assert [item.url for item in gallery.items] == [
        "attachment://frontal.png",
        "attachment://lateral.jpg",
    ]
    assert [item.description for item in gallery.items] == ["frontal.png", "lateral.jpg"]
    header = container.items[0]
    assert isinstance(header, discord.ui.TextDisplay)
    assert "BIT-100000" in header.content
    assert "$150.000,00" in header.content
    footer = container.items[-1]
    assert isinstance(footer, discord.ui.TextDisplay)
    assert "3 adjunto(s)" in footer.content
    assert "<@321>" in footer.content
    download = view.children[1]
    assert isinstance(download, discord.ui.File)
    assert download.url == "attachment://informe.pdf"


def test_logbook_gallery_requires_at_least_two_images() -> None:
    with pytest.raises(ValueError, match="at least two images"):
        build_entry_gallery_view(
            _entry(),
            files=(LogbookFile("frontal.png", "image/png", b"front"),),
            output_filenames=("frontal.png",),
            attachment_count=1,
        )


def test_personnel_supports_a_general_disciplinary_action() -> None:
    personnel_subtypes = dict(CATEGORY_SUBTYPES[LogbookCategory.PERSONNEL])

    assert personnel_subtypes["disciplinary_action"] == "Acción disciplinaria"


def test_additional_character_authorization_belongs_only_to_personnel() -> None:
    key = "additional_character_authorization"
    personnel_subtypes = dict(available_subtypes(LogbookCategory.PERSONNEL, None))

    assert personnel_subtypes[key] == "Autorización de personaje adicional"
    assert all(
        key not in dict(subtypes)
        for category, subtypes in CATEGORY_SUBTYPES.items()
        if category is not LogbookCategory.PERSONNEL
    )


def test_additional_character_authorization_renders_in_the_entry_title() -> None:
    entry = _entry().model_copy(
        update={
            "category": LogbookCategory.PERSONNEL,
            "subtype": "additional_character_authorization",
            "subject": "Charles Harrington",
            "description": "Se autoriza el personaje adicional James Carter.",
            "money_direction": None,
            "money_amount": None,
            "money_detail": None,
        }
    )

    embed = build_entry_embed(entry, attachment_count=0)

    assert embed.title == f"👤 {entry.reference} · Autorización de personaje adicional"
    assert "James Carter" in (embed.description or "")


def test_assignments_use_graduation_for_completed_ftp() -> None:
    assignment_subtypes = dict(CATEGORY_SUBTYPES[LogbookCategory.ASSIGNMENTS])

    assert assignment_subtypes["ftp_end"] == "Graduación del FTP"


def test_finance_subtypes_follow_the_selected_money_direction() -> None:
    income = dict(available_subtypes(LogbookCategory.FINANCE, MoneyDirection.INCOME))
    withdrawal = dict(available_subtypes(LogbookCategory.FINANCE, MoneyDirection.WITHDRAWAL))

    assert "deposit" in income
    assert "withdrawal" not in income
    assert "payment" not in income
    assert "withdrawal" in withdrawal
    assert "payment" in withdrawal
    assert "deposit" not in withdrawal
    assert available_subtypes(LogbookCategory.FINANCE, None) == ()


def test_guide_lists_every_category_and_its_current_subtypes(persona) -> None:
    embed = build_guide_embed(persona.messages.interface.logbook)

    assert embed.title == "📚 Guía de tipos de bitácora"
    assert len(embed.fields) == len(LogbookCategory)
    assert all(field.inline for field in embed.fields)
    fields = {field.name: field.value for field in embed.fields}
    assert "↳ **Ingreso:** Depósito · Reembolso · Ajuste · Otro" in fields["💵 Finanzas"]
    assert "↳ **Retiro:** Retiro · Pago · Reembolso · Ajuste · Otro" in fields["💵 Finanzas"]
    assert "↳ Ingreso · Reincorporación" in fields["👤 Personal"]
    assert "Acción disciplinaria" in fields["👤 Personal"]
    assert "Autorización de personaje adicional" in fields["👤 Personal"]
    assert "Entrega de equipo" in fields["🪪 Licencias y equipo"]
    assert fields["📝 Otro"] == "↳ Otro registro"


@pytest.mark.asyncio
@pytest.mark.parametrize("channel_id", [456, 999])
async def test_logbook_guide_opens_visibility_selector_only_in_allowed_channels(
    persona,
    channel_id: int,
) -> None:
    responses: list[dict[str, object]] = []
    modals: list[LogbookGuideModal] = []

    async def cached(_guild_id: int):
        return SimpleNamespace(
            snapshot=SimpleNamespace(features={"logbook": SimpleNamespace(enabled=True)}),
            settings=SimpleNamespace(logbook_channel_ids=(456,)),
            persona=SimpleNamespace(profile=persona),
        )

    async def respond(**kwargs: object) -> None:
        responses.append(kwargs)

    async def send_modal(modal: LogbookGuideModal) -> None:
        modals.append(modal)

    cog = LogbookCog(SimpleNamespace(cached=cached), None, persona)  # type: ignore[arg-type]
    context = SimpleNamespace(
        guild=SimpleNamespace(id=123),
        channel=SimpleNamespace(id=channel_id),
        author=SimpleNamespace(id=321),
        respond=respond,
        send_modal=send_modal,
    )

    await LogbookCog.guide.callback(cog, context)  # type: ignore[arg-type]

    if channel_id == 999:
        assert responses[0]["ephemeral"] is True
        embed = responses[0]["embed"]
        assert embed.title == persona.messages.interface.logbook.alert_title
        assert embed.description == persona.messages.interface.logbook.wrong_channel
    else:
        assert responses == []
        assert len(modals) == 1
        assert modals[0].publish.default is False


@pytest.mark.asyncio
@pytest.mark.parametrize("publish", [False, True])
async def test_logbook_guide_visibility_is_selected_in_the_modal(persona, publish: bool) -> None:
    responses: list[dict[str, object]] = []

    async def cached(_guild_id: int):
        return SimpleNamespace(
            snapshot=SimpleNamespace(features={"logbook": SimpleNamespace(enabled=True)}),
            settings=SimpleNamespace(logbook_channel_ids=(456,)),
            persona=SimpleNamespace(profile=persona),
        )

    async def send_message(**kwargs: object) -> None:
        responses.append(kwargs)

    cog = LogbookCog(SimpleNamespace(cached=cached), None, persona)  # type: ignore[arg-type]
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=321),
        guild=SimpleNamespace(id=123),
        channel=SimpleNamespace(id=456),
        response=SimpleNamespace(send_message=send_message),
    )

    await cog.show_guide(  # type: ignore[arg-type]
        interaction,
        requester_id=321,
        guild_id=123,
        channel_id=456,
        publish=publish,
    )

    assert responses[0]["ephemeral"] is (not publish)
    assert responses[0]["embed"].title == "📚 Guía de tipos de bitácora"


@pytest.mark.asyncio
async def test_service_stores_content_addressed_assets_once() -> None:
    repository = FakeRepository()
    storage = MemoryObjectStorage()
    service = LogbookService(repository, storage)  # type: ignore[arg-type]
    file = LogbookFile(filename="respaldo.png", content_type="image/png", payload=b"image")

    first = await service.store_files(
        _entry(number=1),
        (file,),
        max_file_bytes=100,
        max_total_bytes=100,
    )
    second = await service.store_files(
        _entry(number=2),
        (file,),
        max_file_bytes=100,
        max_total_bytes=100,
    )

    assert len(storage.objects) == 1
    assert first[0].object_key == second[0].object_key
    assert first[0].sha256 == second[0].sha256


@pytest.mark.asyncio
async def test_service_enforces_attachment_limits_before_upload() -> None:
    service = LogbookService(FakeRepository(), MemoryObjectStorage())  # type: ignore[arg-type]

    with pytest.raises(LogbookFileLimitError):
        await service.store_files(
            _entry(),
            (LogbookFile(filename="large.bin", content_type=None, payload=b"123"),),
            max_file_bytes=2,
            max_total_bytes=10,
        )


@pytest.mark.asyncio
async def test_publish_failure_logs_discord_context_and_recovers_private_notice(
    persona,
    caplog,
) -> None:
    entry = _entry().model_copy(
        update={
            "category": LogbookCategory.EQUIPMENT,
            "subtype": "license_revocation",
        }
    )
    repository = SimpleNamespace(
        reserve=AsyncMock(return_value=(entry, True)),
        mark_failed=AsyncMock(),
        mark_published=AsyncMock(),
    )
    service = SimpleNamespace(
        repository=repository,
        store_files=AsyncMock(return_value=(SimpleNamespace(),)),
    )

    async def cached(_guild_id: int):
        return SimpleNamespace(
            snapshot=SimpleNamespace(features={"logbook": SimpleNamespace(enabled=True)}),
            settings=SimpleNamespace(
                logbook_channel_ids=(456,),
                max_file_bytes=10_000,
                max_total_file_bytes=20_000,
            ),
            persona=SimpleNamespace(profile=persona),
        )

    forbidden = discord.Forbidden(
        SimpleNamespace(status=403, reason="Forbidden"),
        {"code": 50013, "message": "Missing Permissions"},
    )
    missing_response = discord.NotFound(
        SimpleNamespace(status=404, reason="Not Found"),
        {"code": 10008, "message": "Unknown Message"},
    )
    channel = SimpleNamespace(
        id=456,
        type=SimpleNamespace(name="text"),
        parent_id=654,
        send=AsyncMock(side_effect=forbidden),
    )
    permissions = discord.Permissions.none()
    permissions.update(
        view_channel=True,
        send_messages=True,
        embed_links=True,
        attach_files=True,
    )
    attachment = SimpleNamespace(
        id=987,
        filename="respaldo.PNG",
        content_type="image/png",
        size=5,
        read=AsyncMock(return_value=b"image"),
    )
    interaction = SimpleNamespace(
        id=789,
        user=SimpleNamespace(id=321),
        guild=SimpleNamespace(id=123),
        channel=channel,
        app_permissions=permissions,
        response=SimpleNamespace(defer=AsyncMock()),
        edit_original_response=AsyncMock(side_effect=missing_response),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    cog = LogbookCog(SimpleNamespace(cached=cached), service, persona)  # type: ignore[arg-type]

    with caplog.at_level(logging.INFO, logger="arrest_report_agent.logbook.discord"):
        await cog.publish(  # type: ignore[arg-type]
            interaction,
            requester_id=321,
            guild_id=123,
            channel_id=456,
            category=LogbookCategory.EQUIPMENT,
            money_direction=None,
            subtype="license_revocation",
            subject="Licencia CCW",
            description="Revocación de licencia.",
            amount_text="",
            attachments=(attachment,),
        )

    repository.mark_failed.assert_awaited_once_with(entry.id)
    interaction.followup.send.assert_awaited_once()
    assert interaction.followup.send.await_args.kwargs["ephemeral"] is True
    events = {
        record.structured_event["event"]: record.structured_event
        for record in caplog.records
        if hasattr(record, "structured_event")
    }
    started = events["logbook_publish_started"]
    assert started["interaction_id"] == 789
    assert started["channel_id"] == 456
    assert started["parent_channel_id"] == 654
    assert started["channel_type"] == "text"
    assert started["attachment_count"] == 1
    assert started["attachment_total_bytes"] == 5
    assert started["attachment_sizes"] == "5"
    assert started["attachment_content_types"] == "image/png"
    assert started["attachment_extensions"] == "png"
    assert started["attachment_ids"] == "987"
    assert started["can_send_messages"] is True
    assert started["can_embed_links"] is True
    assert started["can_attach_files"] is True
    failed = events["logbook_publish_failed"]
    assert failed["entry_id"] == str(entry.id)
    assert failed["failure_stage"] == "send_message"
    assert failed["error_status"] == 403
    assert failed["error_code"] == 50013
    notice_failure = events["logbook_failure_notice_edit_failed"]
    assert notice_failure["error_status"] == 404
    assert notice_failure["error_code"] == 10008
    assert events["logbook_failure_notice_recovered"]["delivery"] == "followup"


@pytest.mark.asyncio
async def test_adapted_modal_only_adds_amount_for_financial_movement() -> None:
    plain = LogbookEntryModal(
        cog=None,  # type: ignore[arg-type]
        requester_id=1,
        guild_id=2,
        channel_id=3,
        category=LogbookCategory.FLEET,
        money_direction=None,
    )
    financial = LogbookEntryModal(
        cog=None,  # type: ignore[arg-type]
        requester_id=1,
        guild_id=2,
        channel_id=3,
        category=LogbookCategory.FLEET,
        money_direction=MoneyDirection.WITHDRAWAL,
    )

    assert len(plain.children) == 4
    assert len(financial.children) == 5


@pytest.mark.asyncio
async def test_personnel_modal_can_submit_an_additional_character_authorization() -> None:
    publish = AsyncMock()
    modal = LogbookEntryModal(
        cog=SimpleNamespace(publish=publish),  # type: ignore[arg-type]
        requester_id=1,
        guild_id=2,
        channel_id=3,
        category=LogbookCategory.PERSONNEL,
        money_direction=None,
    )
    options = {option.value: option.label for option in modal.subtype.options}
    assert options["additional_character_authorization"] == "Autorización de personaje adicional"
    interaction = SimpleNamespace(data={})
    modal.subtype.refresh_from_modal(
        interaction, {"values": ["additional_character_authorization"]}
    )
    modal.subject.value = "Charles Harrington"
    modal.description.value = "Se autoriza el personaje adicional James Carter."
    await modal.callback(interaction)  # type: ignore[arg-type]

    publish.assert_awaited_once_with(
        interaction,
        requester_id=1,
        guild_id=2,
        channel_id=3,
        category=LogbookCategory.PERSONNEL,
        money_direction=None,
        subtype="additional_character_authorization",
        subject="Charles Harrington",
        description="Se autoriza el personaje adicional James Carter.",
        amount_text="",
        attachments=(),
    )


@pytest.mark.asyncio
async def test_finance_modal_never_offers_a_contradictory_movement_subtype() -> None:
    income = LogbookEntryModal(
        cog=None,  # type: ignore[arg-type]
        requester_id=1,
        guild_id=2,
        channel_id=3,
        category=LogbookCategory.FINANCE,
        money_direction=MoneyDirection.INCOME,
    )
    withdrawal = LogbookEntryModal(
        cog=None,  # type: ignore[arg-type]
        requester_id=1,
        guild_id=2,
        channel_id=3,
        category=LogbookCategory.FINANCE,
        money_direction=MoneyDirection.WITHDRAWAL,
    )

    assert "withdrawal" not in {option.value for option in income.subtype.options}
    assert "payment" not in {option.value for option in income.subtype.options}
    assert "deposit" not in {option.value for option in withdrawal.subtype.options}


def test_summary_embed_is_compact_and_calculates_financial_balance() -> None:
    embed = build_summary_embed(
        _summary(),
        requester_name="Michael Hayes",
        requester_avatar_url="https://cdn.discordapp.com/avatars/1/avatar.png",
    )

    assert embed.title == "📋 Resumen de bitácora"
    assert embed.author.name == "Consultado por Michael Hayes"
    assert embed.author.icon_url == "https://cdn.discordapp.com/avatars/1/avatar.png"
    assert "01/08/2026 — 24/08/2026" in embed.description
    assert [field.name for field in embed.fields[:3]] == [
        "📝 Registros",
        "📎 Adjuntos",
        "🏷️ Tipos",
    ]
    fields = {field.name: field.value for field in embed.fields}
    assert fields["Distribución"].startswith("💵 Finanzas **7** · 👤 Personal **3**")
    assert "💵 Depósito **5**" in fields["Tipos principales"]
    assert "📥 $500.000,00" in fields["💵 Movimientos financieros"]
    assert "📤 $650.000,00" in fields["💵 Movimientos financieros"]
    assert "Balance -$150.000,00" in fields["💵 Movimientos financieros"]


def test_empty_summary_omits_distribution_and_financial_sections() -> None:
    summary = _summary().model_copy(
        update={
            "total_entries": 0,
            "attachment_count": 0,
            "income_total": Decimal(0),
            "withdrawal_total": Decimal(0),
            "categories": (),
            "subtypes": (),
        }
    )

    embed = build_summary_embed(
        summary,
        requester_name="Michael Hayes",
        requester_avatar_url="https://cdn.discordapp.com/avatars/1/avatar.png",
    )

    assert [field.name for field in embed.fields] == [
        "📝 Registros",
        "📎 Adjuntos",
        "🏷️ Tipos",
        "Resultado",
    ]


@pytest.mark.asyncio
async def test_summary_setup_offers_dependent_subtypes_and_all_filters() -> None:
    view = LogbookSummarySetupView(
        cog=None,  # type: ignore[arg-type]
        requester_id=1,
        guild_id=2,
        channel_id=3,
    )

    assert view.category.options[0].value == "all"
    assert view.category.options[0].default is True
    assert view.subtype.disabled is True
    view.select_category(LogbookCategory.FINANCE)
    assert next(option for option in view.category.options if option.default).value == "finance"
    assert view.category.placeholder == "Finanzas"
    assert view.subtype.disabled is False
    assert view.subtype.options[0].value == "all"
    assert {option.value for option in view.subtype.options} == {
        "all",
        *(key for key, _label in CATEGORY_SUBTYPES[LogbookCategory.FINANCE]),
    }
    view.select_category(None)
    assert next(option for option in view.category.options if option.default).value == "all"
    assert view.category.placeholder == "Todas las categorías"
    assert view.subtype.disabled is True
    assert [option.value for option in view.subtype.options] == ["all"]


@pytest.mark.asyncio
async def test_summary_setup_offers_additional_character_authorization_under_personnel() -> None:
    view = LogbookSummarySetupView(
        cog=None,  # type: ignore[arg-type]
        requester_id=1,
        guild_id=2,
        channel_id=3,
    )

    view.select_category(LogbookCategory.PERSONNEL)

    options = {option.value: option.label for option in view.subtype.options}
    assert options["additional_character_authorization"] == "Autorización de personaje adicional"


@pytest.mark.asyncio
async def test_summary_modal_defaults_to_public_and_maps_both_visibilities() -> None:
    captured: list[LogbookSummaryVisibility] = []

    async def publish_summary(_interaction: object, **kwargs: object) -> None:
        captured.append(LogbookSummaryVisibility(str(kwargs["visibility"])))

    modal = LogbookSummaryModal(
        cog=SimpleNamespace(publish_summary=publish_summary),  # type: ignore[arg-type]
        requester_id=1,
        guild_id=2,
        channel_id=3,
        category=None,
        subtype=None,
    )

    assert modal.publish.default is True
    assert [option.value for option in modal.period.options] == [
        "1",
        "2",
        "7",
        "14",
        "30",
        "90",
        "custom",
    ]
    modal.publish.refresh_state({"value": True})
    await modal.callback(SimpleNamespace())  # type: ignore[arg-type]
    modal.publish.refresh_state({"value": False})
    await modal.callback(SimpleNamespace())  # type: ignore[arg-type]

    assert captured == [LogbookSummaryVisibility.PUBLIC, LogbookSummaryVisibility.PRIVATE]


@pytest.mark.asyncio
async def test_summary_command_uses_leadership_roles_in_any_channel(persona) -> None:
    denied_responses: list[dict[str, object]] = []
    allowed_responses: list[dict[str, object]] = []
    denied = _member(11, (999,))
    allowed = _member(12, (200,))

    async def cached(_guild_id: int):
        return SimpleNamespace(
            snapshot=SimpleNamespace(features={"logbook": SimpleNamespace(enabled=True)}),
            settings=SimpleNamespace(
                faction_report_role_ids=(200,),
                logbook_channel_ids=(456,),
            ),
            persona=SimpleNamespace(profile=persona),
        )

    async def deny(**kwargs: object) -> None:
        denied_responses.append(kwargs)

    async def allow(**kwargs: object) -> None:
        allowed_responses.append(kwargs)

    cog = LogbookCog(SimpleNamespace(cached=cached), None, persona)  # type: ignore[arg-type]
    await LogbookCog.summary.callback(  # type: ignore[arg-type]
        cog,
        SimpleNamespace(
            guild=denied.guild,
            channel=SimpleNamespace(id=999),
            author=denied,
            respond=deny,
        ),
    )
    await LogbookCog.summary.callback(  # type: ignore[arg-type]
        cog,
        SimpleNamespace(
            guild=allowed.guild,
            channel=SimpleNamespace(id=999),
            author=allowed,
            respond=allow,
        ),
    )

    assert denied_responses[0]["ephemeral"] is True
    assert denied_responses[0]["embed"].description == (
        persona.messages.interface.faction_reports.unauthorized
    )
    assert allowed_responses[0]["ephemeral"] is True
    assert isinstance(allowed_responses[0]["view"], LogbookSummarySetupView)


class _SummaryResult:
    def __init__(self, *, one: dict[str, object] | None = None, rows=()) -> None:
        self._one = one
        self._rows = rows

    def mappings(self):
        return self

    def one(self):
        if self._one is None:
            raise AssertionError("Expected one result")
        return self._one

    def all(self):
        return self._rows


class _SummaryConnection:
    def __init__(self) -> None:
        self.statements: list[object] = []

    async def execute(self, statement):
        self.statements.append(statement)
        if len(self.statements) == 1:
            return _SummaryResult(
                one={
                    "total_entries": 10,
                    "attachment_count": 4,
                    "income_total": Decimal("500000.00"),
                    "withdrawal_total": Decimal("650000.00"),
                }
            )
        return _SummaryResult(
            rows=(
                {"category_key": "finance", "subtype_key": "deposit", "entry_count": 5},
                {"category_key": "personnel", "subtype_key": "entry", "entry_count": 3},
                {
                    "category_key": "finance",
                    "subtype_key": "withdrawal",
                    "entry_count": 2,
                },
            )
        )


class _SummaryClient:
    def __init__(self) -> None:
        self.connection_value = _SummaryConnection()

    @asynccontextmanager
    async def connection(self):
        yield self.connection_value


@pytest.mark.asyncio
async def test_repository_summary_uses_two_aggregates_and_sorts_counts() -> None:
    client = _SummaryClient()
    repository = LogbookRepository(client)  # type: ignore[arg-type]

    result = await repository.summarize(
        guild_id=123,
        date_from=date(2026, 8, 1),
        date_to=date(2026, 8, 24),
    )

    assert len(client.connection_value.statements) == 2
    assert result.total_entries == 10
    assert result.balance == Decimal("-150000.00")
    assert [(item.category, item.count) for item in result.categories] == [
        (LogbookCategory.FINANCE, 7),
        (LogbookCategory.PERSONNEL, 3),
    ]
    assert [item.count for item in result.subtypes] == [5, 3, 2]


@pytest.mark.asyncio
async def test_repository_rejects_subtype_without_matching_category() -> None:
    repository = LogbookRepository(_SummaryClient())  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="require a category"):
        await repository.summarize(
            guild_id=123,
            date_from=date(2026, 8, 1),
            date_to=date(2026, 8, 24),
            subtype="deposit",
        )
    with pytest.raises(ValueError, match="does not belong"):
        await repository.summarize(
            guild_id=123,
            date_from=date(2026, 8, 1),
            date_to=date(2026, 8, 24),
            category=LogbookCategory.FLEET,
            subtype="deposit",
        )


def test_enabled_logbook_requires_an_explicit_channel_policy(settings) -> None:
    agent = GuildAgentConfig(
        model=settings.openai_model,
        reasoning_effort=settings.openai_reasoning_effort,
        review_timeout_seconds=settings.review_timeout_seconds,
        max_agent_turns=settings.max_agent_turns,
        max_images_per_review=settings.max_images_per_review,
        max_image_bytes=settings.max_image_bytes,
        max_files_per_review=settings.max_files_per_review,
        max_file_bytes=settings.max_file_bytes,
        max_total_file_bytes=settings.max_total_file_bytes,
        thread_context_max_messages=settings.thread_context_max_messages,
        channel_context_max_messages=settings.channel_context_max_messages,
        thread_context_max_chars=settings.thread_context_max_chars,
        thread_context_max_message_chars=settings.thread_context_max_message_chars,
        thread_context_max_historical_images=settings.thread_context_max_historical_images,
        thread_context_max_historical_files=settings.thread_context_max_historical_files,
        thread_auto_archive_minutes=settings.thread_auto_archive_minutes,
    )
    policies = {
        key: AccessPolicySnapshot(
            key=key,
            subject_type=POLICY_SUBJECT_TYPES[key],
            mode=AccessPolicyMode.DISABLED,
        )
        for key in AccessPolicyKey
    }
    values = {
        "guild_id": 123,
        "revision_id": uuid.uuid4(),
        "revision": 1,
        "agent": agent,
        "policies": policies,
        "features": {
            "logbook": GuildFeatureSnapshot(
                key="logbook", enabled=True, schema_version=1, payload={}
            )
        },
        "persona_revision_id": uuid.uuid4(),
        "persona_id": "walter_vigil",
        "persona_revision": 1,
    }

    with pytest.raises(ValueError, match="restricted logbook channels"):
        GuildConfigSnapshot.model_validate(values)

    policies[AccessPolicyKey.LOGBOOK_CHANNELS] = AccessPolicySnapshot(
        key=AccessPolicyKey.LOGBOOK_CHANNELS,
        subject_type="channel",
        mode=AccessPolicyMode.RESTRICTED,
        ids=(456,),
    )
    snapshot = GuildConfigSnapshot.model_validate(values)
    assert GuildRuntimeSettings(settings, snapshot).logbook_channel_ids == (456,)

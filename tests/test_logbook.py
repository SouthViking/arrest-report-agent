from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

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
    LogbookEntry,
    LogbookFile,
    LogbookFileLimitError,
    LogbookService,
    LogbookStatus,
    MoneyDirection,
    available_subtypes,
    build_entry_embed,
    build_guide_embed,
    parse_money_amount,
)
from arrest_report_agent.logbook.discord import LogbookCog, LogbookEntryModal, LogbookGuideModal


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


def test_personnel_supports_a_general_disciplinary_action() -> None:
    personnel_subtypes = dict(CATEGORY_SUBTYPES[LogbookCategory.PERSONNEL])

    assert personnel_subtypes["disciplinary_action"] == "Acción disciplinaria"


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

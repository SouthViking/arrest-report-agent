from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

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


def test_summary_embed_is_compact_and_calculates_financial_balance() -> None:
    embed = build_summary_embed(_summary())

    assert embed.title == "📋 Resumen de bitácora"
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

    embed = build_summary_embed(summary)

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

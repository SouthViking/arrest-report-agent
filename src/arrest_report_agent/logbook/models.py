"""Domain models and catalogue for structured guild logbook entries."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class LogbookCategory(StrEnum):
    FINANCE = "finance"
    PERSONNEL = "personnel"
    ASSIGNMENTS = "assignments"
    EQUIPMENT = "equipment"
    ORDERS = "orders"
    EVIDENCE = "evidence"
    FLEET = "fleet"
    OTHER = "other"


class MoneyDirection(StrEnum):
    INCOME = "income"
    WITHDRAWAL = "withdrawal"


class LogbookStatus(StrEnum):
    PUBLISHING = "publishing"
    PUBLISHED = "published"
    FAILED = "failed"
    VOIDED = "voided"


class LogbookAsset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    entry_id: UUID
    position: int = Field(ge=0)
    object_key: str = Field(min_length=1, max_length=500)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(min_length=1, max_length=100)
    size_bytes: int = Field(gt=0)
    created_at: datetime


class LogbookEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    guild_id: int = Field(gt=0)
    number: int = Field(gt=0)
    channel_id: int = Field(gt=0)
    message_id: int | None = Field(default=None, gt=0)
    interaction_id: int = Field(gt=0)
    author_user_id: int = Field(gt=0)
    category: LogbookCategory
    subtype: str = Field(min_length=1, max_length=40)
    subject: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=2000)
    money_direction: MoneyDirection | None = None
    money_amount: Decimal | None = Field(default=None, gt=0)
    money_detail: str | None = Field(default=None, max_length=300)
    status: LogbookStatus
    version: int = Field(ge=1)
    occurred_at: datetime
    created_at: datetime
    updated_at: datetime
    assets: tuple[LogbookAsset, ...] = ()

    @property
    def reference(self) -> str:
        return f"BIT-{self.number:05d}"


class PendingLogbookAsset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    object_key: str = Field(min_length=1, max_length=500)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(min_length=1, max_length=100)
    size_bytes: int = Field(gt=0)


class LogbookCategoryCount(BaseModel):
    """Number of published entries for one category within a summary filter."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    category: LogbookCategory
    count: int = Field(ge=0)


class LogbookSubtypeCount(BaseModel):
    """Number of published entries for one category/subtype pair."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    category: LogbookCategory
    subtype: str = Field(min_length=1, max_length=40)
    count: int = Field(ge=0)


class LogbookSummary(BaseModel):
    """Compact deterministic projection of published logbook entries."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    guild_id: int = Field(gt=0)
    date_from: date
    date_to: date
    category_filter: LogbookCategory | None = None
    subtype_filter: str | None = Field(default=None, min_length=1, max_length=40)
    total_entries: int = Field(ge=0)
    attachment_count: int = Field(ge=0)
    income_total: Decimal = Field(ge=0)
    withdrawal_total: Decimal = Field(ge=0)
    categories: tuple[LogbookCategoryCount, ...] = ()
    subtypes: tuple[LogbookSubtypeCount, ...] = ()

    @property
    def balance(self) -> Decimal:
        return self.income_total - self.withdrawal_total


CATEGORY_SUBTYPES: dict[LogbookCategory, tuple[tuple[str, str], ...]] = {
    LogbookCategory.FINANCE: (
        ("deposit", "Depósito"),
        ("withdrawal", "Retiro"),
        ("payment", "Pago"),
        ("reimbursement", "Reembolso"),
        ("adjustment", "Ajuste"),
        ("other", "Otro"),
    ),
    LogbookCategory.PERSONNEL: (
        ("entry", "Ingreso"),
        ("reinstatement", "Reincorporación"),
        ("voluntary_departure", "Salida voluntaria"),
        ("expulsion", "Expulsión"),
        ("suspension", "Suspensión"),
        ("disciplinary_action", "Acción disciplinaria"),
        ("return", "Regreso"),
        ("name_change", "Cambio de nombre"),
        ("additional_character_authorization", "Autorización de personaje adicional"),
        ("other", "Otro"),
    ),
    LogbookCategory.ASSIGNMENTS: (
        ("promotion", "Ascenso"),
        ("demotion", "Descenso"),
        ("station_transfer", "Traslado de estación"),
        ("unit_join", "Ingreso a unidad"),
        ("unit_leave", "Salida de unidad"),
        ("ftp_start", "Inicio de FTP"),
        ("ftp_end", "Graduación del FTP"),
        ("special_assignment", "Asignación especial"),
        ("other", "Otro"),
    ),
    LogbookCategory.EQUIPMENT: (
        ("license_issue", "Entrega de licencia"),
        ("license_return", "Devolución de licencia"),
        ("license_revocation", "Revocación de licencia"),
        ("equipment_issue", "Entrega de equipo"),
        ("equipment_return", "Devolución de equipo"),
        ("special_authorization", "Autorización especial"),
        ("other", "Otro"),
    ),
    LogbookCategory.ORDERS: (
        ("arrest_warrant", "Orden de arresto"),
        ("search_warrant", "Orden de registro"),
        ("arrest_completed", "Arresto realizado"),
        ("closure", "Cierre"),
        ("cancellation", "Cancelación"),
        ("pending_action", "Acción pendiente"),
        ("other", "Otro"),
    ),
    LogbookCategory.EVIDENCE: (
        ("withdrawal", "Retiro"),
        ("delivery", "Entrega"),
        ("transfer", "Transferencia"),
        ("return", "Devolución"),
        ("incident", "Pérdida o incidente"),
        ("other", "Otro"),
    ),
    LogbookCategory.FLEET: (
        ("purchase", "Compra"),
        ("sale", "Venta"),
        ("assignment", "Asignación"),
        ("repair", "Reparación o mejora"),
        ("installation", "Instalación o mobiliario"),
        ("asset", "Activo"),
        ("other", "Otro"),
    ),
    LogbookCategory.OTHER: (("other", "Otro registro"),),
}


FINANCE_SUBTYPES_BY_DIRECTION: dict[MoneyDirection, tuple[tuple[str, str], ...]] = {
    MoneyDirection.INCOME: (
        ("deposit", "Depósito"),
        ("reimbursement", "Reembolso"),
        ("adjustment", "Ajuste"),
        ("other", "Otro"),
    ),
    MoneyDirection.WITHDRAWAL: (
        ("withdrawal", "Retiro"),
        ("payment", "Pago"),
        ("reimbursement", "Reembolso"),
        ("adjustment", "Ajuste"),
        ("other", "Otro"),
    ),
}


def available_subtypes(
    category: LogbookCategory,
    money_direction: MoneyDirection | None,
) -> tuple[tuple[str, str], ...]:
    """Return only subtypes coherent with the selected category and money direction."""

    if category is LogbookCategory.FINANCE:
        if money_direction is None:
            return ()
        return FINANCE_SUBTYPES_BY_DIRECTION[money_direction]
    return CATEGORY_SUBTYPES[category]


def subtype_label(category: LogbookCategory, key: str) -> str:
    return dict(CATEGORY_SUBTYPES[category]).get(key, "Otro")

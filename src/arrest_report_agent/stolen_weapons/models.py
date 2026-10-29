"""Validated theft data and durable references to its evidence."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from arrest_report_agent.domain.weapon_serials import normalize_serial
from arrest_report_agent.infrastructure.assets import StoredAsset

__all__ = [
    "MAX_EVIDENCE_IMAGES",
    "MAX_DESCRIPTION_LENGTH",
    "TheftDetails",
    "StolenWeaponReport",
    "StolenWeaponReportLink",
    "normalize_serial",
]

MAX_EVIDENCE_IMAGES = 5
MAX_DESCRIPTION_LENGTH = 2000


def _name(value: str, label: str) -> str:
    name = " ".join(value.split())
    if not 2 <= len(name) <= 100:
        raise ValueError(f"{label} debe tener entre 2 y 100 caracteres.")
    return name


@dataclass(frozen=True, slots=True)
class TheftDetails:
    serial: str
    normalized_serial: str
    description: str
    stolen_on: date
    victim_name: str
    reporting_officer_name: str

    @classmethod
    def parse(
        cls,
        *,
        serial: str,
        description: str,
        stolen_on: str,
        victim_name: str,
        reporting_officer_name: str,
    ) -> TheftDetails:
        if not 1 <= len(serial.strip()) <= 100:
            raise ValueError("El serial debe tener entre 1 y 100 caracteres.")
        normalized = normalize_serial(serial)
        description = description.strip()
        if not 10 <= len(description) <= MAX_DESCRIPTION_LENGTH:
            raise ValueError("Los hechos deben tener entre 10 y 2000 caracteres.")
        try:
            if not re.fullmatch(r"\d{2}/\d{2}/\d{4}", stolen_on.strip()):
                raise ValueError
            day = datetime.strptime(stolen_on.strip(), "%d/%m/%Y").date()
        except ValueError as error:
            raise ValueError("Usa una fecha de robo válida con formato DD/MM/AAAA.") from error
        return cls(
            serial=serial.strip(),
            normalized_serial=normalized,
            description=description,
            stolen_on=day,
            victim_name=_name(victim_name, "El nombre del propietario"),
            reporting_officer_name=_name(reporting_officer_name, "El nombre del ayudante"),
        )


@dataclass(frozen=True, slots=True)
class StolenWeaponReport:
    id: UUID
    submission_id: int
    guild_id: int
    source_channel_id: int
    destination_channel_id: int
    registered_by: int
    details: TheftDetails
    created_at: datetime
    publication_status: str
    message_id: int | None
    assets: tuple[StoredAsset, ...]
    status: str = "open"
    closed_by: int | None = None
    closed_at: datetime | None = None
    closure_reason: str | None = None
    closure_interaction_id: int | None = None

    @property
    def reference(self) -> str:
        return f"ARM-{self.id.hex[:12].upper()}"

    @property
    def jump_url(self) -> str | None:
        if self.message_id is None:
            return None
        return f"https://discord.com/channels/{self.guild_id}/{self.destination_channel_id}/{self.message_id}"


@dataclass(frozen=True, slots=True)
class StolenWeaponReportLink:
    """Minimal public projection of an open theft, without identities or narratives."""

    serial: str
    reference: str
    jump_url: str | None

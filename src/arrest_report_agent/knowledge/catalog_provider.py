"""Atomic snapshots for the shared charge catalog."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from .catalog import ChargeCatalog


class ChargeCatalogSource(StrEnum):
    LOCAL = "local"
    SARP = "sarp"
    SARP_CACHED = "sarp_cached"


@dataclass(frozen=True, slots=True)
class ChargeCatalogSnapshot:
    catalog: ChargeCatalog
    source: ChargeCatalogSource


class ChargeCatalogProvider(Protocol):
    """Return one immutable catalog snapshot without performing I/O."""

    def snapshot(self) -> ChargeCatalogSnapshot: ...


class StaticChargeCatalogProvider:
    """Expose the packaged catalog through the same provider contract."""

    def __init__(
        self,
        catalog: ChargeCatalog,
        source: ChargeCatalogSource = ChargeCatalogSource.LOCAL,
    ) -> None:
        self._snapshot = ChargeCatalogSnapshot(catalog=catalog, source=source)

    def snapshot(self) -> ChargeCatalogSnapshot:
        return self._snapshot


def as_charge_catalog_provider(
    value: ChargeCatalog | ChargeCatalogProvider,
) -> ChargeCatalogProvider:
    if isinstance(value, ChargeCatalog):
        return StaticChargeCatalogProvider(value)
    return value

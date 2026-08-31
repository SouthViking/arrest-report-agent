"""Cached database-backed configuration and persona providers."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from arrest_report_agent.application import ReviewService
from arrest_report_agent.config import (
    AppSettings,
    GuildConfigSnapshot,
    GuildRuntimeSettings,
    LoadedPersona,
    ModelCatalog,
    ResolvedModel,
)
from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.knowledge import (
    ChargeCatalog,
    ChargeCatalogProvider,
    ManualCatalog,
    RadioCodeCatalog,
    ReportGuide,
    as_charge_catalog_provider,
)

from .client import PostgresClient
from .repository import CONFIG_NOTIFY_CHANNEL, ConfigurationRepository

if TYPE_CHECKING:
    from arrest_report_agent.application.ports import EvidenceBagLookup
    from arrest_report_agent.faction_directory import FactionDirectoryService
    from arrest_report_agent.infrastructure.sarp import SarpClient

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class GuildRuntimeBundle:
    snapshot: GuildConfigSnapshot
    settings: GuildRuntimeSettings
    persona: LoadedPersona
    model: ResolvedModel
    service: ReviewService


class DatabaseConfigProvider:
    """Serve immutable per-guild runtime bundles with loss-tolerant invalidation."""

    def __init__(
        self,
        client: PostgresClient,
        repository: ConfigurationRepository,
        process_settings: AppSettings,
        catalog: ChargeCatalog | ChargeCatalogProvider,
        guide: ReportGuide,
        model_catalog: ModelCatalog,
        radio_catalog: RadioCodeCatalog | None = None,
        sarp_client: SarpClient | None = None,
        faction_directory: FactionDirectoryService | None = None,
        evidence_bag_lookup: EvidenceBagLookup | None = None,
        manual_catalog: ManualCatalog | None = None,
    ) -> None:
        self.client = client
        self.repository = repository
        self.process_settings = process_settings
        self.catalog_provider = as_charge_catalog_provider(catalog)
        self.guide = guide
        self.radio_catalog = radio_catalog or RadioCodeCatalog.load_default()
        self.manual_catalog = manual_catalog or ManualCatalog.load_default()
        self.model_catalog = model_catalog
        self.sarp_client = sarp_client
        self.faction_directory = faction_directory
        self.evidence_bag_lookup = evidence_bag_lookup
        # Import lazily so repositories can import the PostgreSQL schema without
        # creating a package-initialization cycle through this provider.
        from arrest_report_agent.arrest_reports import ArrestReportBindingRepository

        self.arrest_bindings = ArrestReportBindingRepository(client)
        self._bundles: dict[int, GuildRuntimeBundle] = {}
        self._personas: dict[UUID, LoadedPersona] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._refresh_task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()
        self._global_callbacks: list[Callable[[LoadedPersona], Awaitable[None]]] = []
        self._guild_callbacks: list[Callable[[GuildRuntimeBundle], Awaitable[None]]] = []

    async def start(self) -> None:
        guild_ids = await self.repository.enabled_guild_ids()
        if not guild_ids:
            raise RuntimeError("PostgreSQL contains no enabled Discord guild configuration")
        for guild_id in guild_ids:
            await self.get(guild_id, force=True)
        self.client.start_listener(CONFIG_NOTIFY_CHANNEL, self._on_notification)
        self._stopped.clear()
        self._refresh_task = asyncio.create_task(
            self._refresh_loop(),
            name="configuration-reconciliation",
        )
        log_event(
            LOGGER,
            "info",
            "database_configuration_loaded",
            configured_guilds=len(guild_ids),
        )

    async def close(self) -> None:
        self._stopped.set()
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            await asyncio.gather(self._refresh_task, return_exceptions=True)
            self._refresh_task = None

    async def guild_ids(self) -> tuple[int, ...]:
        return await self.repository.enabled_guild_ids()

    async def global_persona(self) -> LoadedPersona:
        return await self.repository.global_persona()

    def register_global_callback(
        self,
        callback: Callable[[LoadedPersona], Awaitable[None]],
    ) -> None:
        self._global_callbacks.append(callback)

    def register_guild_callback(
        self,
        callback: Callable[[GuildRuntimeBundle], Awaitable[None]],
    ) -> None:
        self._guild_callbacks.append(callback)

    async def get(self, guild_id: int, *, force: bool = False) -> GuildRuntimeBundle:
        lock = self._locks.setdefault(guild_id, asyncio.Lock())
        async with lock:
            snapshot = await self.repository.active_snapshot(guild_id)
            current = self._bundles.get(guild_id)
            if not force and current is not None and current.snapshot.revision_id == snapshot.revision_id:
                return current
            persona = self._personas.get(snapshot.persona_revision_id)
            if persona is None:
                persona = await self.repository.load_persona_revision(snapshot.persona_revision_id)
                self._personas[snapshot.persona_revision_id] = persona
            settings = GuildRuntimeSettings(self.process_settings, snapshot)
            model = self.model_catalog.resolve(
                snapshot.agent.model,
                snapshot.agent.reasoning_effort,
            )
            arrest_lookup = None
            character_legal_facts_lookup = None
            faction_frequency_lookup = None
            faction_id = settings.arrest_report_lookup_faction_id
            if self.sarp_client is not None and faction_id is not None:
                from arrest_report_agent.arrest_reports import SarpArrestReportLookup

                arrest_lookup = SarpArrestReportLookup(
                    self.sarp_client,
                    self.arrest_bindings,
                    faction_id=faction_id,
                )
                from arrest_report_agent.character_legal_facts import (
                    SarpCharacterLegalFactsLookup,
                )

                character_legal_facts_lookup = SarpCharacterLegalFactsLookup(self.sarp_client)
                if self.faction_directory is not None:
                    from arrest_report_agent.faction_directory import (
                        SarpFactionFrequencyLookup,
                    )

                    faction_frequency_lookup = SarpFactionFrequencyLookup(
                        self.faction_directory,
                        faction_id=faction_id,
                    )
            service = ReviewService(
                settings,  # type: ignore[arg-type]
                self.catalog_provider,
                self.guide,
                model,
                persona.profile,
                self.radio_catalog,
                arrest_lookup,
                character_legal_facts_lookup,
                evidence_bag_lookup=(self.evidence_bag_lookup if faction_id is not None else None),
                faction_frequency_lookup=faction_frequency_lookup,
                manual_catalog=self.manual_catalog,
            )
            bundle = GuildRuntimeBundle(
                snapshot=snapshot,
                settings=settings,
                persona=persona,
                model=model,
                service=service,
            )
            self._bundles[guild_id] = bundle
            for callback in self._guild_callbacks:
                await callback(bundle)
            log_event(
                LOGGER,
                "info",
                "guild_configuration_activated",
                guild_id=guild_id,
                config_revision=snapshot.revision,
                config_revision_id=str(snapshot.revision_id),
                persona_id=persona.profile.id,
                persona_revision=persona.profile.revision,
                model=model.id,
                reasoning_effort=model.reasoning_effort,
            )
            return bundle

    async def cached(self, guild_id: int) -> GuildRuntimeBundle:
        current = self._bundles.get(guild_id)
        if current is not None:
            return current
        return await self.get(guild_id)

    async def invalidate(self, guild_id: int) -> None:
        try:
            await self.get(guild_id, force=True)
        except Exception as error:
            log_event(
                LOGGER,
                "error",
                "guild_configuration_reload_failed",
                exc_info=error,
                guild_id=guild_id,
                **exception_fields(error),
            )

    async def _on_notification(self, payload: str) -> None:
        try:
            decoded = json.loads(payload)
            if decoded.get("global_profile") is True:
                persona = await self.global_persona()
                for callback in self._global_callbacks:
                    await callback(persona)
                return
            guild_id = int(decoded["guild_id"])
        except (TypeError, ValueError, KeyError, json.JSONDecodeError) as error:
            log_event(
                LOGGER,
                "warning",
                "configuration_notification_invalid",
                exc_info=error,
                **exception_fields(error),
            )
            return
        await self.invalidate(guild_id)

    async def _refresh_loop(self) -> None:
        while not self._stopped.is_set():
            try:
                await asyncio.wait_for(
                    self._stopped.wait(),
                    timeout=self.process_settings.config_refresh_seconds,
                )
                return
            except TimeoutError:
                pass
            try:
                guild_ids = await self.repository.enabled_guild_ids()
                for guild_id in guild_ids:
                    await self.get(guild_id)
            except Exception as error:
                log_event(
                    LOGGER,
                    "warning",
                    "configuration_reconciliation_failed",
                    exc_info=error,
                    **exception_fields(error),
                )

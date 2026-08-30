"""Wire local knowledge, agent runner and Discord adapters."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import discord
from agents import set_default_openai_key

from arrest_report_agent.ci_notifications import (
    CiNotificationRepository,
    CiNotificationWorker,
    GitHubWebhookReceiver,
)
from arrest_report_agent.config import (
    AppSettings,
    LoadedPersona,
    ModelCatalog,
    PersonaProvider,
    ResolvedModel,
    load_global_profile_banner,
)
from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.cost_monitoring import (
    CostMonitoringService,
    build_cost_monitoring_service,
)
from arrest_report_agent.dedeo import DedeoCog
from arrest_report_agent.deployments import (
    DeploymentNotificationWorker,
    DeploymentRepository,
    RailwayWebhookReceiver,
)
from arrest_report_agent.evidence_bags import EvidenceBagCog, EvidenceBagService
from arrest_report_agent.faction_directory import FactionDirectoryService
from arrest_report_agent.faction_member_analysis import (
    FactionMemberAnalysisCog,
    FactionMemberAnalysisService,
)
from arrest_report_agent.faction_reports import FactionReportCog, FactionReportService
from arrest_report_agent.faction_status import FactionStatusCog, FactionStatusService
from arrest_report_agent.forum_shortcuts import ForumShortcutsCog
from arrest_report_agent.ftp_requests import (
    FtpRequestCog,
    FtpRequestControlsView,
    FtpRequestRepository,
    FtpRequestServiceRegistry,
)
from arrest_report_agent.ftp_status import FtpStatusCog, FtpStatusService
from arrest_report_agent.infrastructure.discord import (
    ConfigurationAdminCog,
    DiscordProfileSynchronizer,
    ReviewCog,
    create_bot,
)
from arrest_report_agent.infrastructure.discord.member_directory import (
    DiscordGuildMemberDirectory,
)
from arrest_report_agent.infrastructure.postgres import (
    ConfigurationRepository,
    DatabaseConfigProvider,
    PostgresClient,
)
from arrest_report_agent.infrastructure.sarp import (
    SarpClient,
    SarpClientConfig,
    SarpPenalCodeProvider,
)
from arrest_report_agent.knowledge import (
    ChargeCatalog,
    ChargeCatalogProvider,
    ManualCatalog,
    RadioCodeCatalog,
    ReportGuide,
    StaticChargeCatalogProvider,
)
from arrest_report_agent.logbook import LogbookCog, LogbookRepository, LogbookService
from arrest_report_agent.manual_quiz import (
    ManualQuizCog,
    ManualQuizDiscordPublisher,
    ManualQuizRepository,
    ManualQuizSessionRegistry,
)
from arrest_report_agent.mdc import MdcCog, MdcService
from arrest_report_agent.operational_profiles import CallsignCog, MemberProfileRepository
from arrest_report_agent.operational_resources import (
    OperationalResourcesCog,
    OperationalResourcesService,
)
from arrest_report_agent.pagers import (
    PagerCog,
    PagerControlsView,
    PagerRepository,
    PagerServiceRegistry,
)
from arrest_report_agent.pending_arrest_reports import (
    PendingArrestReportCog,
    PendingArrestReportService,
)
from arrest_report_agent.polls import DiscordPollService, PollEventCog, PollRepository
from arrest_report_agent.radio_audit import (
    RadioAuditCog,
    RadioLogService,
    RadioManualCatalog,
)
from arrest_report_agent.release import ReleaseManifest
from arrest_report_agent.report_owner_access import ReportOwnerRunRepository
from arrest_report_agent.review_analytics import ReportReviewRepository
from arrest_report_agent.role_requests import (
    RoleRequestCog,
    RoleRequestDecisionView,
    RoleRequestRepository,
    RoleRequestResolutionView,
    RoleRequestService,
    RoleRequestServiceRegistry,
)
from arrest_report_agent.service_calls import ServiceCallFeedWorker, ServiceCallRepository
from arrest_report_agent.stolen_vehicles import (
    StolenVehicleCog,
    StolenVehicleFeedRepository,
    StolenVehicleFeedWorker,
    StolenVehicleService,
)
from arrest_report_agent.suggestions import SuggestionCog, SuggestionRepository
from arrest_report_agent.support import SupportCog
from arrest_report_agent.warrant_notifications import (
    WarrantLookupService,
    WarrantNotificationCog,
    WarrantNotificationView,
)

from .database import build_bucket_storage, build_postgres_client

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Runtime:
    bot: discord.Bot
    catalog_provider: ChargeCatalogProvider
    sarp_penal_code_provider: SarpPenalCodeProvider | None
    guide: ReportGuide
    radio_catalog: RadioCodeCatalog
    model: ResolvedModel
    persona: LoadedPersona
    role_requests_enabled: bool
    pagers_enabled: bool
    suggestions_enabled: bool
    logbook_enabled: bool
    deployment_notifications_enabled: bool
    deployment_worker: DeploymentNotificationWorker
    railway_webhook_receiver: RailwayWebhookReceiver | None
    ci_notification_worker: CiNotificationWorker
    github_webhook_receiver: GitHubWebhookReceiver | None
    cost_monitoring: CostMonitoringService | None
    service_call_worker: ServiceCallFeedWorker | None
    stolen_vehicle_worker: StolenVehicleFeedWorker | None
    postgres: PostgresClient
    config_provider: DatabaseConfigProvider

    @property
    def catalog(self) -> ChargeCatalog:
        """Return the current shared charge catalog snapshot."""

        return self.catalog_provider.snapshot().catalog

    async def health(self) -> dict[str, object]:
        discord_ready = self.bot.is_ready()
        catalog_snapshot = self.catalog_provider.snapshot()
        knowledge_ready = (
            catalog_snapshot.catalog.charge_count > 0
            and bool(self.guide.version)
            and self.radio_catalog.entry_count > 0
        )
        try:
            database_ready = await self.postgres.ping()
        except Exception:
            database_ready = False
        return {
            "ok": discord_ready and knowledge_ready and database_ready,
            "discord": discord_ready,
            "database": database_ready,
            "knowledge": knowledge_ready,
            "charges": catalog_snapshot.catalog.charge_count,
            "charge_catalog_source": catalog_snapshot.source.value,
            "sarp_api": self.sarp_penal_code_provider is not None,
            "radio_codes": self.radio_catalog.entry_count,
            "persona": self.persona.profile.id,
            "persona_revision": self.persona.profile.revision,
            "role_requests": self.role_requests_enabled,
            "pagers": self.pagers_enabled,
            "suggestions": self.suggestions_enabled,
            "logbook": self.logbook_enabled,
            "deployment_notifications": self.deployment_notifications_enabled,
            "railway_webhook": self.railway_webhook_receiver is not None,
            "github_webhook": self.github_webhook_receiver is not None,
            "service_calls": (
                self.service_call_worker.running if self.service_call_worker is not None else False
            ),
            "stolen_vehicles": (
                self.stolen_vehicle_worker.running if self.stolen_vehicle_worker is not None else False
            ),
        }

    async def close(self) -> None:
        await self.deployment_worker.close()
        await self.ci_notification_worker.close()
        if self.cost_monitoring is not None:
            await self.cost_monitoring.close()
        if self.service_call_worker is not None:
            await self.service_call_worker.close()
        if self.stolen_vehicle_worker is not None:
            await self.stolen_vehicle_worker.close()
        await self.config_provider.close()
        if self.sarp_penal_code_provider is not None:
            await self.sarp_penal_code_provider.close()
        await self.postgres.close()


async def build_runtime(
    settings: AppSettings,
    persona_provider: PersonaProvider | None = None,
) -> Runtime:
    if persona_provider is not None:
        raise ValueError("Runtime personas are authoritative in PostgreSQL")
    release_manifest = ReleaseManifest.load_packaged()
    postgres = build_postgres_client(settings)
    await postgres.open()
    local_catalog = ChargeCatalog.load_default()
    sarp_penal_code_provider: SarpPenalCodeProvider | None = None
    sarp_client: SarpClient | None = None
    faction_directory: FactionDirectoryService | None = None
    evidence_bag_service: EvidenceBagService | None = None
    if settings.sarp_api_enabled:
        sarp_client = SarpClient(SarpClientConfig.from_settings(settings))
        faction_directory = FactionDirectoryService(sarp_client)
        evidence_bag_service = EvidenceBagService(sarp_client)
        sarp_penal_code_provider = SarpPenalCodeProvider(
            sarp_client,
            local_catalog,
        )
        catalog_provider: ChargeCatalogProvider = sarp_penal_code_provider
    else:
        catalog_provider = StaticChargeCatalogProvider(local_catalog)
    catalog_snapshot = catalog_provider.snapshot()
    log_event(
        LOGGER,
        "info",
        "charge_catalog_provider_initialized",
        sarp_api_enabled=settings.sarp_api_enabled,
        active_source=catalog_snapshot.source.value,
        charge_count=catalog_snapshot.catalog.charge_count,
        source_hash=catalog_snapshot.catalog.source_hash,
    )
    guide = ReportGuide.load_default()
    manual_catalog = ManualCatalog.load_default()
    radio_catalog = RadioCodeCatalog.load_default()
    model_catalog = ModelCatalog.load_default()
    object_storage = build_bucket_storage(settings)
    repository = ConfigurationRepository(postgres, object_storage)
    config_provider = DatabaseConfigProvider(
        postgres,
        repository,
        settings,
        catalog_provider,
        guide,
        model_catalog,
        radio_catalog,
        sarp_client=sarp_client,
        faction_directory=faction_directory,
        evidence_bag_lookup=evidence_bag_service,
        manual_catalog=manual_catalog,
    )
    try:
        if sarp_penal_code_provider is not None:
            await sarp_penal_code_provider.start()
        await config_provider.start()
        persona = await config_provider.global_persona()
        guild_ids = await config_provider.guild_ids()
        initial_bundle = await config_provider.get(guild_ids[0])
    except Exception:
        await config_provider.close()
        if sarp_penal_code_provider is not None:
            await sarp_penal_code_provider.close()
        await postgres.close()
        raise
    log_event(
        LOGGER,
        "info",
        "persona_loaded",
        persona_id=persona.profile.id,
        persona_revision=persona.profile.revision,
        persona_schema_version=persona.profile.schema_version,
    )
    set_default_openai_key(settings.openai_api_key.get_secret_value(), use_for_tracing=True)
    model = initial_bundle.model
    bot = create_bot(settings, persona.profile, guild_ids=guild_ids)
    profile_synchronizer = DiscordProfileSynchronizer(
        bot,
        persona,
        global_banner_bytes=load_global_profile_banner(),
    )
    report_review_repository = ReportReviewRepository(postgres)
    report_owner_run_repository = ReportOwnerRunRepository(postgres)
    manual_quiz_repository = ManualQuizRepository(postgres)
    manual_quiz_sessions = ManualQuizSessionRegistry(manual_quiz_repository)
    manual_quiz_publisher = ManualQuizDiscordPublisher(manual_quiz_sessions)
    poll_repository = PollRepository(postgres)
    poll_service = DiscordPollService(bot, poll_repository)
    review_cog = ReviewCog(
        bot,
        initial_bundle.settings,  # type: ignore[arg-type]
        initial_bundle.service,
        initial_bundle.persona.profile,
        profile_synchronizer,
        config_provider=config_provider,
        report_review_repository=report_review_repository,
        report_owner_run_repository=report_owner_run_repository,
        manual_quiz_publisher=manual_quiz_publisher,
        poll_manager=poll_service,
    )
    bot.add_cog(review_cog)
    bot.add_cog(PollEventCog(poll_repository))
    cost_monitoring = build_cost_monitoring_service(settings)
    all_bundles = [await config_provider.get(guild_id) for guild_id in guild_ids]
    logbook_enabled = any(
        (feature := bundle.snapshot.features.get("logbook")) is not None and feature.enabled
        for bundle in all_bundles
    )
    bot.add_cog(
        ConfigurationAdminCog(
            settings,
            repository,
            config_provider,
            cost_monitoring,
        )
    )

    async def synchronize_global_persona(updated: LoadedPersona) -> None:
        await profile_synchronizer.select_persona(updated)
        try:
            await repository.record_global_profile_sync(
                synchronized=profile_synchronizer.global_profile_synchronized,
                error="Discord rejected one or more managed profile fields",
            )
        except Exception as error:
            log_event(
                LOGGER,
                "warning",
                "global_profile_sync_result_write_failed",
                exc_info=error,
                **exception_fields(error),
            )
        presence = updated.profile.presence
        await review_cog.presence.update_texts(
            idle_text=presence.idle,
            conversation_text=presence.conversation,
            knowledge_text=presence.knowledge,
            manual_index_text=presence.manual_index,
            manual_search_text=presence.manual_search,
            manual_read_text=presence.manual_read,
            frequencies_text=presence.frequencies,
            personnel_text=presence.personnel,
            records_text=presence.records,
            evidence_text=presence.evidence,
            evidence_bag_text=presence.evidence_bag,
            review_text=presence.review,
            response_text=presence.response,
        )

    config_provider.register_global_callback(synchronize_global_persona)
    service_call_worker: ServiceCallFeedWorker | None = None
    stolen_vehicle_worker: StolenVehicleFeedWorker | None = None
    stolen_vehicle_service: StolenVehicleService | None = None
    if sarp_client is not None:
        service_call_guild_ids = tuple(
            bundle.snapshot.guild_id
            for bundle in all_bundles
            if (feature := bundle.snapshot.features.get("service_calls")) is not None
            and feature.enabled
            and bundle.settings.service_call_channel_ids
        )
        service_call_worker = ServiceCallFeedWorker(
            bot,
            config_provider,
            ServiceCallRepository(postgres),
            sarp_client,
            poll_seconds=settings.sarp_emergency_call_poll_seconds,
            enabled_guild_ids=service_call_guild_ids,
        )
        config_provider.register_guild_callback(service_call_worker.configuration_updated)
        service_call_worker.start()
        log_event(
            LOGGER,
            "info",
            "service_call_subsystem_registered",
            configured_guilds=len(service_call_guild_ids),
            running=service_call_worker.running,
            poll_seconds=settings.sarp_emergency_call_poll_seconds,
        )
        stolen_vehicle_service = StolenVehicleService(sarp_client)
        stolen_vehicle_guild_ids = tuple(
            bundle.snapshot.guild_id
            for bundle in all_bundles
            if (feature := bundle.snapshot.features.get("stolen_vehicles")) is not None
            and feature.enabled
            and bundle.settings.stolen_vehicle_channel_ids
        )
        stolen_vehicle_worker = StolenVehicleFeedWorker(
            bot,
            config_provider,
            StolenVehicleFeedRepository(postgres),
            stolen_vehicle_service,
            poll_seconds=settings.sarp_stolen_vehicle_poll_seconds,
            enabled_guild_ids=stolen_vehicle_guild_ids,
        )
        config_provider.register_guild_callback(stolen_vehicle_worker.configuration_updated)
        stolen_vehicle_worker.start()
        log_event(
            LOGGER,
            "info",
            "stolen_vehicle_subsystem_registered",
            configured_guilds=len(stolen_vehicle_guild_ids),
            running=stolen_vehicle_worker.running,
            poll_seconds=settings.sarp_stolen_vehicle_poll_seconds,
        )
    role_request_bundles = [
        bundle
        for bundle in all_bundles
        if (feature := bundle.snapshot.features.get("role_requests")) is not None and feature.enabled
    ]
    role_requests_enabled = bool(role_request_bundles)
    default_bundle = role_request_bundles[0] if role_request_bundles else initial_bundle
    role_request_repository = RoleRequestRepository(postgres)
    role_request_service = RoleRequestService(
        bot,
        default_bundle.settings,
        default_bundle.persona.profile,
        repository=role_request_repository,
    )
    registry = RoleRequestServiceRegistry(bot, config_provider, role_request_repository)
    role_request_service.registry = registry
    bot.add_view(RoleRequestDecisionView(role_request_service, registry=registry))
    bot.add_view(RoleRequestResolutionView(role_request_service, registry=registry))
    bot.add_cog(RoleRequestCog(role_request_service, registry=registry))
    log_event(
        LOGGER,
        "info",
        "role_request_subsystem_registered",
        configured_guilds=len(role_request_bundles),
    )
    pager_bundles = [
        bundle
        for bundle in all_bundles
        if (feature := bundle.snapshot.features.get("pagers")) is not None and feature.enabled
    ]
    pager_repository = PagerRepository(postgres)
    member_profile_repository = MemberProfileRepository(postgres)
    pager_registry = PagerServiceRegistry(
        bot,
        config_provider,
        pager_repository,
        member_profile_repository,
    )
    bot.pager_registry = pager_registry  # type: ignore[attr-defined]
    bot.add_view(PagerControlsView())
    bot.add_cog(PagerCog(pager_registry, persona.profile))
    ftp_request_bundles = [
        bundle
        for bundle in all_bundles
        if (feature := bundle.snapshot.features.get("ftp_requests")) is not None and feature.enabled
    ]
    ftp_request_registry = FtpRequestServiceRegistry(
        bot,
        config_provider,
        FtpRequestRepository(postgres),
    )
    bot.ftp_request_registry = ftp_request_registry  # type: ignore[attr-defined]
    bot.add_view(FtpRequestControlsView())
    bot.add_cog(FtpRequestCog(ftp_request_registry, persona.profile))
    log_event(
        LOGGER,
        "info",
        "ftp_request_subsystem_registered",
        configured_guilds=len(ftp_request_bundles),
    )
    bot.add_cog(CallsignCog(config_provider, member_profile_repository, persona.profile))
    bot.add_cog(
        ManualQuizCog(
            config_provider,
            manual_quiz_sessions,
            persona.profile,
            manual_quiz_publisher,
        )
    )
    bot.add_cog(ForumShortcutsCog(config_provider))
    bot.add_cog(DedeoCog(persona.profile))
    bot.add_cog(SupportCog())
    if sarp_client is not None:
        if evidence_bag_service is None:
            raise RuntimeError("Evidence-bag service was not initialized")
        if stolen_vehicle_service is None:
            raise RuntimeError("Stolen-vehicle service was not initialized")
        if faction_directory is None:
            raise RuntimeError("Faction-directory service was not initialized")
        warrant_lookup = WarrantLookupService(sarp_client)
        bot.add_view(WarrantNotificationView(warrant_lookup))
        bot.add_cog(WarrantNotificationCog(config_provider, warrant_lookup))
        faction_status_service = FactionStatusService(
            sarp_client,
            directory=faction_directory,
        )
        bot.add_cog(MdcCog(config_provider, MdcService(sarp_client), persona.profile))
        bot.add_cog(
            EvidenceBagCog(
                config_provider,
                evidence_bag_service,
                persona.profile,
            )
        )
        bot.add_cog(
            FactionStatusCog(
                config_provider,
                faction_status_service,
                persona.profile,
            )
        )
        bot.add_cog(
            FtpStatusCog(
                config_provider,
                FtpStatusService(
                    faction_directory,
                    DiscordGuildMemberDirectory(bot),
                ),
                persona.profile,
            )
        )
        bot.add_cog(
            FactionMemberAnalysisCog(
                config_provider,
                FactionMemberAnalysisService(faction_directory),
                persona.profile,
            )
        )
        bot.add_cog(
            OperationalResourcesCog(
                config_provider,
                OperationalResourcesService(faction_status_service, sarp_client),
                persona.profile,
            )
        )
        bot.add_cog(
            PendingArrestReportCog(
                config_provider,
                PendingArrestReportService(sarp_client),
                persona.profile,
            )
        )
        bot.add_cog(
            FactionReportCog(
                config_provider,
                FactionReportService(sarp_client, faction_directory),
                persona.profile,
            )
        )
        bot.add_cog(
            StolenVehicleCog(
                config_provider,
                stolen_vehicle_service,
                persona.profile,
            )
        )
        bot.add_cog(
            RadioAuditCog(
                config_provider,
                RadioLogService(sarp_client),
                radio_catalog,
                RadioManualCatalog.load_default(),
                persona.profile,
            )
        )
        log_event(
            LOGGER,
            "info",
            "sarp_command_subsystems_registered",
            configured_guilds=len(guild_ids),
        )
    log_event(
        LOGGER,
        "info",
        "manual_quiz_subsystem_registered",
        configured_guilds=len(guild_ids),
    )
    pagers_enabled = bool(pager_bundles)
    log_event(
        LOGGER,
        "info",
        "pager_subsystem_registered",
        configured_guilds=len(pager_bundles),
    )
    suggestion_repository = SuggestionRepository(postgres)
    bot.add_cog(
        SuggestionCog(
            settings,
            config_provider,
            suggestion_repository,
            persona.profile,
        )
    )
    suggestions_enabled = bool(guild_ids)
    log_event(
        LOGGER,
        "info",
        "suggestion_subsystem_registered",
        configured_guilds=len(guild_ids),
        configured_owners=len(settings.config_owner_user_ids),
    )
    bot.add_cog(
        LogbookCog(
            config_provider,
            LogbookService(LogbookRepository(postgres), object_storage),
            persona.profile,
        )
    )
    log_event(
        LOGGER,
        "info",
        "logbook_subsystem_registered",
        configured_guilds=sum(
            1
            for bundle in all_bundles
            if (feature := bundle.snapshot.features.get("logbook")) is not None and feature.enabled
        ),
    )
    deployment_repository = DeploymentRepository(postgres)
    deployment_worker = DeploymentNotificationWorker(
        bot,
        config_provider,
        deployment_repository,
        release_manifest,
        current_commit_sha=settings.railway_git_commit_sha,
    )
    deployment_worker.start()
    railway_webhook_receiver = (
        RailwayWebhookReceiver(
            settings,
            deployment_repository,
            deployment_worker.wake,
        )
        if settings.railway_webhook_enabled
        else None
    )
    ci_notification_repository = CiNotificationRepository(postgres)
    ci_notification_worker = CiNotificationWorker(
        bot,
        config_provider,
        ci_notification_repository,
        release_manifest,
    )
    ci_notification_worker.start()
    github_webhook_receiver = (
        GitHubWebhookReceiver(
            settings,
            ci_notification_repository,
            ci_notification_worker.wake,
            expected_repository=release_manifest.repository_full_name,
        )
        if settings.github_webhook_enabled
        else None
    )
    deployment_bundles = [
        bundle
        for bundle in all_bundles
        if (feature := bundle.snapshot.features.get("deployment_notifications")) is not None
        and feature.enabled
    ]
    deployment_notifications_enabled = bool(deployment_bundles)
    log_event(
        LOGGER,
        "info",
        "deployment_notification_subsystem_registered",
        configured_guilds=len(deployment_bundles),
        webhook_enabled=railway_webhook_receiver is not None,
        release_version=release_manifest.version,
        github_webhook_enabled=github_webhook_receiver is not None,
    )
    return Runtime(
        bot=bot,
        catalog_provider=catalog_provider,
        sarp_penal_code_provider=sarp_penal_code_provider,
        guide=guide,
        radio_catalog=radio_catalog,
        model=model,
        persona=persona,
        role_requests_enabled=role_requests_enabled,
        pagers_enabled=pagers_enabled,
        suggestions_enabled=suggestions_enabled,
        logbook_enabled=logbook_enabled,
        deployment_notifications_enabled=deployment_notifications_enabled,
        deployment_worker=deployment_worker,
        railway_webhook_receiver=railway_webhook_receiver,
        ci_notification_worker=ci_notification_worker,
        github_webhook_receiver=github_webhook_receiver,
        cost_monitoring=cost_monitoring,
        service_call_worker=service_call_worker,
        stolen_vehicle_worker=stolen_vehicle_worker,
        postgres=postgres,
        config_provider=config_provider,
    )

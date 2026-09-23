"""Database bootstrap, environment import and verification commands."""

from __future__ import annotations

import asyncio
import hashlib
import json
import mimetypes
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from arrest_report_agent.config import (
    AccessPolicyKey,
    AccessPolicyMode,
    AccessPolicySnapshot,
    AppSettings,
    GuildAgentConfig,
    GuildFeatureSnapshot,
    PackagedPersonaProvider,
)
from arrest_report_agent.config.bootstrap_defaults import ftp_defaults, role_request_review_defaults
from arrest_report_agent.infrastructure.postgres import (
    ConfigurationRepository,
    PostgresClient,
    PostgresClientConfig,
)
from arrest_report_agent.infrastructure.postgres.repository import BootstrapGuild
from arrest_report_agent.infrastructure.storage import (
    BucketConfig,
    ObjectStorage,
    RailwayBucketStorage,
)

BOOTSTRAP_MIGRATION_NAME = "bootstrap_env_v1"
LSSD_GUILD_ID = 1234674521416663070
LSSD_COMMAND_AND_EXECUTIVE_ROLE_IDS = (
    1234678029662294047,
    1234678238182244414,
)


class BootstrapError(RuntimeError):
    """Raised when the legacy configuration cannot be migrated safely."""


@dataclass(frozen=True, slots=True)
class DiscordGuildInfo:
    guild_id: int
    name: str


@dataclass(frozen=True, slots=True)
class EnvironmentImportPlan:
    checksum: str
    guilds: tuple[BootstrapGuild, ...]
    persona_ids: tuple[str, ...]
    active_persona_id: str

    def public_summary(self) -> dict[str, Any]:
        return {
            "migration": BOOTSTRAP_MIGRATION_NAME,
            "checksum": self.checksum,
            "active_persona": self.active_persona_id,
            "personas": list(self.persona_ids),
            "guilds": [
                {
                    "guild_id": item.guild_id,
                    "name": item.name,
                    "model": item.agent.model,
                    "reasoning_effort": item.agent.reasoning_effort,
                    "policies": {
                        policy.key.value: {
                            "mode": policy.mode.value,
                            "count": len(policy.ids),
                        }
                        for policy in item.policies
                    },
                    "features": {feature.key: feature.enabled for feature in item.features},
                }
                for item in self.guilds
            ],
        }


class DiscordBootstrapResolver:
    """Read Discord metadata without starting the production gateway client."""

    API_BASE = "https://discord.com/api/v10"

    def __init__(self, bot_token: str) -> None:
        self.bot_token = bot_token

    async def guilds(self) -> tuple[DiscordGuildInfo, ...]:
        payload = await self._get("/users/@me/guilds")
        if not isinstance(payload, list):
            raise BootstrapError("Discord returned an invalid guild list")
        return tuple(
            DiscordGuildInfo(guild_id=int(item["id"]), name=str(item.get("name") or item["id"]))
            for item in payload
        )

    async def channel_guild(self, channel_id: int) -> int:
        payload = await self._get(f"/channels/{channel_id}")
        try:
            return int(payload["guild_id"])
        except (KeyError, TypeError, ValueError) as error:
            raise BootstrapError(f"Channel {channel_id} does not belong to a guild") from error

    async def role_owners(self, guild_ids: tuple[int, ...]) -> dict[int, int]:
        owners: dict[int, int] = {}
        for guild_id in guild_ids:
            payload = await self._get(f"/guilds/{guild_id}/roles")
            if not isinstance(payload, list):
                raise BootstrapError(f"Discord returned invalid roles for guild {guild_id}")
            for item in payload:
                owners[int(item["id"])] = guild_id
        return owners

    async def _get(self, path: str) -> Any:
        def request() -> Any:
            message = urllib.request.Request(
                self.API_BASE + path,
                headers={
                    "Authorization": f"Bot {self.bot_token}",
                    "User-Agent": "arrest-report-agent-bootstrap/1.0",
                },
            )
            try:
                with urllib.request.urlopen(message, timeout=20) as response:
                    return json.load(response)
            except urllib.error.HTTPError as error:
                body = error.read(500).decode("utf-8", errors="replace")
                raise BootstrapError(
                    f"Discord API rejected bootstrap metadata request ({error.code}): {body}"
                ) from error
            except (OSError, json.JSONDecodeError) as error:
                raise BootstrapError("Unable to read bootstrap metadata from Discord") from error

        return await asyncio.to_thread(request)


def build_postgres_client(settings: AppSettings) -> PostgresClient:
    return PostgresClient(
        PostgresClientConfig(
            url=settings.postgres_url,
            pool_size=settings.database_pool_size,
            max_overflow=settings.database_max_overflow,
            pool_timeout_seconds=settings.database_pool_timeout_seconds,
            pool_recycle_seconds=settings.database_pool_recycle_seconds,
            statement_timeout_ms=settings.database_statement_timeout_ms,
        )
    )


def build_bucket_storage(settings: AppSettings) -> RailwayBucketStorage:
    if not settings.bucket_is_configured:
        raise BootstrapError(
            "Railway Bucket variables are required: PERSONA_ASSET_BUCKET, "
            "PERSONA_ASSET_ENDPOINT, PERSONA_ASSET_ACCESS_KEY_ID and "
            "PERSONA_ASSET_SECRET_ACCESS_KEY"
        )
    assert settings.bucket_name is not None
    assert settings.bucket_endpoint is not None
    assert settings.bucket_access_key_id is not None
    assert settings.bucket_secret_access_key is not None
    return RailwayBucketStorage(
        BucketConfig(
            bucket=settings.bucket_name,
            endpoint_url=settings.bucket_endpoint,
            region=settings.bucket_region,
            access_key_id=settings.bucket_access_key_id.get_secret_value(),
            secret_access_key=settings.bucket_secret_access_key.get_secret_value(),
        )
    )


async def plan_environment_import(settings: AppSettings) -> EnvironmentImportPlan:
    if settings.bot_persona is None:
        raise BootstrapError("BOT_PERSONA is required only while importing legacy env data")
    resolver = DiscordBootstrapResolver(settings.discord_token.get_secret_value())
    visible_guilds = await resolver.guilds()
    visible_by_id = {item.guild_id: item for item in visible_guilds}
    configured_ids = settings.guild_allowlist.ids
    guild_ids = tuple(sorted(configured_ids or visible_by_id))
    if not guild_ids:
        raise BootstrapError("The bot is not connected to any Discord guild")
    missing_guilds = set(guild_ids) - set(visible_by_id)
    if missing_guilds:
        raise BootstrapError(
            "Configured guilds are unavailable to the bot: "
            + ", ".join(str(item) for item in sorted(missing_guilds))
        )
    role_owners = await resolver.role_owners(guild_ids)

    async def partition_channels(ids: frozenset[int] | None) -> dict[int, tuple[int, ...]]:
        result: dict[int, list[int]] = {guild_id: [] for guild_id in guild_ids}
        if ids is None:
            return {guild_id: () for guild_id in guild_ids}
        for channel_id in sorted(ids):
            owner = await resolver.channel_guild(channel_id)
            if owner not in result:
                raise BootstrapError(f"Channel {channel_id} belongs to non-configured guild {owner}")
            result[owner].append(channel_id)
        return {guild_id: tuple(values) for guild_id, values in result.items()}

    def partition_roles(ids: tuple[int, ...] | frozenset[int] | None) -> dict[int, tuple[int, ...]]:
        result: dict[int, list[int]] = {guild_id: [] for guild_id in guild_ids}
        if ids is None:
            return {guild_id: () for guild_id in guild_ids}
        for role_id in ids:
            owner = role_owners.get(role_id)
            if owner is None:
                raise BootstrapError(f"Role {role_id} was not found in a configured guild")
            result[owner].append(role_id)
        return {guild_id: tuple(values) for guild_id, values in result.items()}

    report_channels = await partition_channels(settings.report_channel_allowlist.ids)
    role_request_channels = await partition_channels(settings.role_request_allowed_channel_allowlist.ids)
    supervisor_roles = partition_roles(settings.supervisor_role_allowlist.ids)
    escalation_roles = partition_roles(settings.review_escalation_role_ids)
    allowed_roles = partition_roles(settings.role_request_allowed_role_allowlist.ids)
    reviewer_roles = partition_roles(settings.role_request_reviewer_role_ids)

    def policy(
        key: AccessPolicyKey,
        subject_type: str,
        source_is_open: bool,
        ids: tuple[int, ...],
        *,
        empty_means_disabled: bool = False,
    ) -> AccessPolicySnapshot:
        if source_is_open:
            mode = AccessPolicyMode.OPEN
            members: tuple[int, ...] = ()
        elif ids:
            mode = AccessPolicyMode.RESTRICTED
            members = ids
        elif empty_means_disabled or not ids:
            mode = AccessPolicyMode.DISABLED
            members = ()
        return AccessPolicySnapshot(
            key=key,
            subject_type=subject_type,  # type: ignore[arg-type]
            mode=mode,
            ids=members,
        )

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
        thread_auto_archive_minutes=settings.thread_auto_archive_minutes,  # type: ignore[arg-type]
    )
    placeholder_persona_revision = UUID(int=0)
    guilds: list[BootstrapGuild] = []
    for guild_id in guild_ids:
        review_defaults = role_request_review_defaults(guild_id)
        configured_ftp = ftp_defaults(guild_id)
        ftp_enabled = configured_ftp is not None and bool(supervisor_roles[guild_id])
        reviewer_ids = (
            review_defaults.reviewer_role_ids if review_defaults is not None else reviewer_roles[guild_id]
        )
        review_channel_ids = review_defaults.channel_ids if review_defaults is not None else ()
        policies = (
            policy(
                AccessPolicyKey.REPORT_CHANNELS,
                "channel",
                not settings.report_channel_allowlist.is_restricted,
                report_channels[guild_id],
            ),
            policy(
                AccessPolicyKey.AGENT_USERS,
                "user",
                not settings.user_allowlist.is_restricted,
                tuple(sorted(settings.user_allowlist.ids or ())),
            ),
            AccessPolicySnapshot(
                key=AccessPolicyKey.AGENT_ROLES,
                subject_type="role",
                mode=AccessPolicyMode.DISABLED,
            ),
            AccessPolicySnapshot(
                key=AccessPolicyKey.AGENT_BLOCKED_USERS,
                subject_type="user",
                mode=AccessPolicyMode.DISABLED,
            ),
            policy(
                AccessPolicyKey.SUPERVISOR_ROLES,
                "role",
                not settings.supervisor_role_allowlist.is_restricted,
                supervisor_roles[guild_id],
            ),
            policy(
                AccessPolicyKey.REPORT_ANALYTICS_ROLES,
                "role",
                not settings.supervisor_role_allowlist.is_restricted,
                supervisor_roles[guild_id],
            ),
            policy(
                AccessPolicyKey.REVIEW_ESCALATION_ROLES,
                "role",
                False,
                escalation_roles[guild_id],
                empty_means_disabled=True,
            ),
            policy(
                AccessPolicyKey.ROLE_REQUEST_CHANNELS,
                "channel",
                not settings.role_request_allowed_channel_allowlist.is_restricted,
                role_request_channels[guild_id],
            ),
            AccessPolicySnapshot(
                key=AccessPolicyKey.ROLE_REQUEST_REVIEW_CHANNELS,
                subject_type="channel",
                mode=(AccessPolicyMode.RESTRICTED if review_channel_ids else AccessPolicyMode.DISABLED),
                ids=review_channel_ids,
            ),
            policy(
                AccessPolicyKey.ROLE_REQUEST_ALLOWED_ROLES,
                "role",
                not settings.role_request_allowed_role_allowlist.is_restricted,
                allowed_roles[guild_id],
            ),
            policy(
                AccessPolicyKey.ROLE_REQUEST_REVIEWER_ROLES,
                "role",
                False,
                reviewer_ids,
                empty_means_disabled=True,
            ),
            AccessPolicySnapshot(
                key=AccessPolicyKey.CONFIG_ADMIN_ROLES,
                subject_type="role",
                mode=AccessPolicyMode.DISABLED,
            ),
            AccessPolicySnapshot(
                key=AccessPolicyKey.PAGER_CHANNELS,
                subject_type="channel",
                mode=AccessPolicyMode.DISABLED,
            ),
            AccessPolicySnapshot(
                key=AccessPolicyKey.PAGER_CREATOR_ROLES,
                subject_type="role",
                mode=AccessPolicyMode.DISABLED,
            ),
            AccessPolicySnapshot(
                key=AccessPolicyKey.PAGER_RECIPIENT_ROLES,
                subject_type="role",
                mode=AccessPolicyMode.DISABLED,
            ),
            AccessPolicySnapshot(
                key=AccessPolicyKey.PAGER_CLOSER_ROLES,
                subject_type="role",
                mode=AccessPolicyMode.DISABLED,
            ),
            policy(
                AccessPolicyKey.OPERATIONAL_STAFF_ROLES,
                "role",
                False,
                supervisor_roles[guild_id],
                empty_means_disabled=True,
            ),
            AccessPolicySnapshot(
                key=AccessPolicyKey.FACTION_REPORT_ROLES,
                subject_type="role",
                mode=(
                    AccessPolicyMode.RESTRICTED if guild_id == LSSD_GUILD_ID else AccessPolicyMode.DISABLED
                ),
                ids=(LSSD_COMMAND_AND_EXECUTIVE_ROLE_IDS if guild_id == LSSD_GUILD_ID else ()),
            ),
            AccessPolicySnapshot(
                key=AccessPolicyKey.DEPLOYMENT_NOTIFICATION_CHANNELS,
                subject_type="channel",
                mode=AccessPolicyMode.DISABLED,
            ),
            AccessPolicySnapshot(
                key=AccessPolicyKey.DEPLOYMENT_INCIDENT_NOTIFICATION_CHANNELS,
                subject_type="channel",
                mode=AccessPolicyMode.DISABLED,
            ),
            AccessPolicySnapshot(
                key=AccessPolicyKey.CI_FAILURE_NOTIFICATION_CHANNELS,
                subject_type="channel",
                mode=AccessPolicyMode.DISABLED,
            ),
            AccessPolicySnapshot(
                key=AccessPolicyKey.SERVICE_CALL_CHANNELS,
                subject_type="channel",
                mode=AccessPolicyMode.DISABLED,
            ),
            AccessPolicySnapshot(
                key=AccessPolicyKey.STOLEN_VEHICLE_CHANNELS,
                subject_type="channel",
                mode=(
                    AccessPolicyMode.RESTRICTED
                    if guild_id == 1234674521416663070
                    else AccessPolicyMode.DISABLED
                ),
                ids=((1539037905014759485,) if guild_id == 1234674521416663070 else ()),
            ),
            AccessPolicySnapshot(
                key=AccessPolicyKey.LOGBOOK_CHANNELS,
                subject_type="channel",
                mode=AccessPolicyMode.DISABLED,
            ),
        )
        guilds.append(
            BootstrapGuild(
                guild_id=guild_id,
                name=visible_by_id[guild_id].name,
                agent=agent,
                policies=policies,
                features=(
                    GuildFeatureSnapshot(
                        key="role_requests",
                        enabled=bool(reviewer_ids),
                        schema_version=1,
                        payload={},
                    ),
                    GuildFeatureSnapshot(
                        key="teamspeak",
                        enabled=True,
                        schema_version=1,
                        payload={"address": "51.81.49.72:11189"},
                    ),
                    GuildFeatureSnapshot(
                        key="pagers",
                        enabled=False,
                        schema_version=1,
                        payload={
                            "starting_number": 92 if guild_id == 1234674521416663070 else 1,
                            **(
                                {"forward_channel_id": 1234675609536430152}
                                if guild_id == 1234674521416663070
                                else {}
                            ),
                        },
                    ),
                    GuildFeatureSnapshot(
                        key="deployment_notifications",
                        enabled=False,
                        schema_version=1,
                        payload={},
                    ),
                    GuildFeatureSnapshot(
                        key="report_owner_access",
                        enabled=guild_id == 1234674521416663070,
                        schema_version=1,
                        payload=({"max_rounds": 5} if guild_id == 1234674521416663070 else {}),
                    ),
                    GuildFeatureSnapshot(
                        key="service_calls",
                        enabled=False,
                        schema_version=1,
                        payload={},
                    ),
                    GuildFeatureSnapshot(
                        key="stolen_vehicles",
                        enabled=(guild_id == 1234674521416663070 and bool(supervisor_roles[guild_id])),
                        schema_version=1,
                        payload={},
                    ),
                    GuildFeatureSnapshot(
                        key="faction_status",
                        enabled=False,
                        schema_version=1,
                        payload={},
                    ),
                    GuildFeatureSnapshot(
                        key="ftp_status",
                        enabled=ftp_enabled,
                        schema_version=1,
                        payload=(
                            {
                                "faction_id": configured_ftp.faction_id,
                                "supervisory_role_ids": list(configured_ftp.supervisory_role_ids),
                                "mfto_role_ids": list(configured_ftp.mfto_role_ids),
                                "fto_role_ids": list(configured_ftp.fto_role_ids),
                                "ftt_role_ids": list(configured_ftp.ftt_role_ids),
                            }
                            if ftp_enabled and configured_ftp is not None
                            else {}
                        ),
                    ),
                    GuildFeatureSnapshot(
                        key="ftp_requests",
                        enabled=ftp_enabled,
                        schema_version=1,
                        payload=(
                            {
                                "channel_id": configured_ftp.request_channel_id,
                                "command_role_ids": list(configured_ftp.command_role_ids),
                                "executive_role_ids": list(configured_ftp.executive_role_ids),
                            }
                            if ftp_enabled and configured_ftp is not None
                            else {}
                        ),
                    ),
                    GuildFeatureSnapshot(
                        key="warrant_notifications",
                        enabled=guild_id == 1234674521416663070,
                        schema_version=1,
                        payload=(
                            {
                                "channel_id": 1552367150591905793,
                                "result_channel_id": 1537203701893169254,
                                "command_role_ids": [1234678029662294047],
                                "executive_role_ids": [1234678238182244414],
                            }
                            if guild_id == 1234674521416663070
                            else {}
                        ),
                    ),
                    GuildFeatureSnapshot(
                        key="faction_member_analysis",
                        enabled=(guild_id == 1234674521416663070 and bool(supervisor_roles[guild_id])),
                        schema_version=1,
                        payload=(
                            {
                                "faction_id": 3,
                                "base_divisions": ["GNT", "DLM"],
                                "inactivity_warning_days": 14,
                                "inactivity_critical_days": 30,
                                "inactivity_warning_percentage": 20,
                                "inactivity_critical_percentage": 30,
                            }
                            if guild_id == 1234674521416663070 and bool(supervisor_roles[guild_id])
                            else {}
                        ),
                    ),
                    GuildFeatureSnapshot(
                        key="operational_resources",
                        enabled=(guild_id == 1234674521416663070 and bool(supervisor_roles[guild_id])),
                        schema_version=1,
                        payload=(
                            {
                                "faction_id": 3,
                                "game_server_address": "play.sarp.es:7777",
                                "pcu_url": "https://ucp.sarp.es/app/ucp/home",
                                "forum_url": (
                                    "https://forum.sarp.es/index.php?/forum/199-foro-los-"
                                    "santos-county-sheriffs-department/"
                                ),
                                "manual_url": (
                                    "https://docs.google.com/document/d/"
                                    "1DrhSgzPbdLoe0ahFTVrNPb_zBPuhVeatxW5Hs8ECMHE/"
                                    "edit?pli=1&tab=t.0#heading=h.thzh3oj2x0rk"
                                ),
                                "penal_code_url": "https://ucp.sarp.es/app/ucp/penalcode",
                            }
                            if guild_id == 1234674521416663070 and bool(supervisor_roles[guild_id])
                            else {}
                        ),
                    ),
                    GuildFeatureSnapshot(
                        key="forum_shortcuts",
                        enabled=guild_id == 1234674521416663070,
                        schema_version=1,
                        payload=(
                            {
                                "organization_name": ("Los Santos Sheriff's Department"),
                                "description": (
                                    "Consulta rápidamente nuestros espacios y procedimientos oficiales."
                                ),
                                "banner_url": "https://i.imgur.com/gBxZZIT.gif",
                                "sections": [
                                    {
                                        "title": "🏛️ Foro institucional",
                                        "links": [
                                            {
                                                "label": "Página principal",
                                                "url": "https://sheriff.sarp.es/index.php",
                                            }
                                        ],
                                    },
                                    {
                                        "title": "👥 Ingreso y desarrollo",
                                        "links": [
                                            {
                                                "label": "Oportunidades de empleo",
                                                "url": ("https://sheriff.sarp.es/viewforum.php?f=3"),
                                            },
                                            {
                                                "label": "Transferencias laterales",
                                                "url": ("https://sheriff.sarp.es/viewtopic.php?t=162"),
                                            },
                                            {
                                                "label": "Reincorporaciones",
                                                "url": ("https://sheriff.sarp.es/viewtopic.php?t=104"),
                                            },
                                            {
                                                "label": "Field Training Program",
                                                "url": ("https://sheriff.sarp.es/viewforum.php?f=10"),
                                            },
                                        ],
                                    },
                                    {
                                        "title": "📋 Solicitudes y participación pública",
                                        "links": [
                                            {
                                                "label": "Quejas y elogios públicos",
                                                "url": ("https://sheriff.sarp.es/viewtopic.php?t=497"),
                                            },
                                            {
                                                "label": "Licencias CCW y de caza",
                                                "url": ("https://sheriff.sarp.es/viewforum.php?f=135"),
                                            },
                                        ],
                                    },
                                ],
                            }
                            if guild_id == 1234674521416663070
                            else {}
                        ),
                    ),
                    GuildFeatureSnapshot(
                        key="arrest_report_lookup",
                        enabled=guild_id == 1234674521416663070,
                        schema_version=1,
                        payload=({"faction_id": 3} if guild_id == 1234674521416663070 else {}),
                    ),
                    GuildFeatureSnapshot(
                        key="pending_arrest_reports",
                        enabled=guild_id == 1234674521416663070,
                        schema_version=1,
                        payload=(
                            {
                                "faction_id": 3,
                                "max_range_days": 90,
                                "max_records": 1000,
                            }
                            if guild_id == 1234674521416663070
                            else {}
                        ),
                    ),
                    GuildFeatureSnapshot(
                        key="arrest_statistics",
                        enabled=guild_id == LSSD_GUILD_ID,
                        schema_version=1,
                        payload=(
                            {
                                "faction_id": 3,
                                "max_range_days": 30,
                                "max_reports": 200,
                                "cache_ttl_seconds": 900,
                                "detail_concurrency": 3,
                                "max_top": 10,
                            }
                            if guild_id == LSSD_GUILD_ID
                            else {}
                        ),
                    ),
                    GuildFeatureSnapshot(
                        key="logbook",
                        enabled=False,
                        schema_version=1,
                        payload={},
                    ),
                    GuildFeatureSnapshot(
                        key="blacklists",
                        enabled=(guild_id == LSSD_GUILD_ID and bool(supervisor_roles[guild_id])),
                        schema_version=1,
                        payload=(
                            {
                                "manager_role_ids": list(LSSD_COMMAND_AND_EXECUTIVE_ROLE_IDS),
                                "types": [
                                    {
                                        "key": "ccw",
                                        "display_name": "CCW",
                                        "reference_prefix": "CCW",
                                        "channel_ids": [1530621801871441991],
                                        "profile_kind": "character",
                                        "max_profiles": 1,
                                        "subject_label": "Personaje",
                                        "enabled": True,
                                    },
                                    {
                                        "key": "faction",
                                        "display_name": "Facción",
                                        "reference_prefix": "FAC",
                                        "channel_ids": [1383926305136971906],
                                        "profile_kind": "account",
                                        "max_profiles": 5,
                                        "subject_label": "Persona",
                                        "enabled": True,
                                    },
                                ],
                            }
                            if guild_id == LSSD_GUILD_ID
                            else {"types": [], "manager_role_ids": []}
                        ),
                    ),
                ),
                persona_revision_id=placeholder_persona_revision,
            )
        )
    checksum_payload = {
        "guilds": [
            {
                "guild_id": item.guild_id,
                "agent": item.agent.model_dump(mode="json"),
                "policies": [entry.model_dump(mode="json") for entry in item.policies],
                "features": [entry.model_dump(mode="json") for entry in item.features],
            }
            for item in guilds
        ],
        "active_persona": settings.bot_persona,
        "personas": ["roy_velazco", "walter_vigil"],
    }
    checksum = hashlib.sha256(
        json.dumps(checksum_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return EnvironmentImportPlan(
        checksum=checksum,
        guilds=tuple(guilds),
        persona_ids=("roy_velazco", "walter_vigil"),
        active_persona_id=settings.bot_persona,
    )


async def apply_environment_import(
    settings: AppSettings,
    plan: EnvironmentImportPlan,
    repository: ConfigurationRepository,
    *,
    replace: bool = False,
) -> dict[str, Any]:
    existing = await repository.migration_record(BOOTSTRAP_MIGRATION_NAME)
    if existing is not None and existing["checksum"] == plan.checksum:
        return {"status": "noop", **plan.public_summary()}
    if existing is not None and not replace:
        raise BootstrapError(
            "bootstrap_env_v1 was already applied with different input; use --replace explicitly"
        )
    packaged = PackagedPersonaProvider()
    loaded = {persona_id: await packaged.load(persona_id) for persona_id in plan.persona_ids}
    persona_records: dict[str, Any] = {}
    async with repository.client.transaction() as transaction:
        for persona_id, persona in loaded.items():
            content_type = mimetypes.guess_type(persona.profile.discord.avatar)[0] or "image/png"
            persona_records[persona_id] = await repository.upsert_persona_revision(
                persona.profile,
                persona.avatar_bytes,
                content_type=content_type,
                connection=transaction,
            )
        active = persona_records.get(plan.active_persona_id)
        if active is None:
            raise BootstrapError(f"BOT_PERSONA '{plan.active_persona_id}' is not part of the import set")
        revisions: dict[str, str] = {}
        for item in plan.guilds:
            revision_id = await repository.bootstrap_guild(
                BootstrapGuild(
                    guild_id=item.guild_id,
                    name=item.name,
                    agent=item.agent,
                    policies=item.policies,
                    features=item.features,
                    persona_revision_id=active.revision_id,
                ),
                connection=transaction,
            )
            revisions[str(item.guild_id)] = str(revision_id)
        await repository.set_global_persona(active.revision_id, connection=transaction)
        details = {
            "guild_count": len(plan.guilds),
            "personas": list(plan.persona_ids),
            "active_persona": plan.active_persona_id,
            "config_revisions": revisions,
        }
        await repository.record_migration(
            BOOTSTRAP_MIGRATION_NAME,
            plan.checksum,
            details,
            connection=transaction,
        )
    return {"status": "applied", **plan.public_summary()}


async def run_import_command(
    settings: AppSettings,
    *,
    apply: bool,
    replace: bool = False,
) -> dict[str, Any]:
    plan = await plan_environment_import(settings)
    if not apply:
        return {"status": "dry_run", **plan.public_summary()}
    client = build_postgres_client(settings)
    await client.open()
    try:
        storage: ObjectStorage = build_bucket_storage(settings)
        repository = ConfigurationRepository(client, storage)
        return await apply_environment_import(settings, plan, repository, replace=replace)
    finally:
        await client.close()


async def run_verify_command(settings: AppSettings) -> dict[str, Any]:
    client = build_postgres_client(settings)
    await client.open()
    try:
        repository = ConfigurationRepository(client, build_bucket_storage(settings))
        return await repository.verify()
    finally:
        await client.close()

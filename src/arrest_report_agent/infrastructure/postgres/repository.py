"""Typed repositories for configuration, personas and bootstrap state."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection

from arrest_report_agent.config import (
    AccessPolicyKey,
    AccessPolicySnapshot,
    BotPersona,
    GuildAgentConfig,
    GuildConfigSnapshot,
    GuildFeatureSnapshot,
    LoadedPersona,
    PersonaConfigurationError,
)
from arrest_report_agent.infrastructure.storage import ObjectStorage

from .client import PostgresClient
from .schema import (
    bot_application_profile,
    config_audit_events,
    data_migrations,
    discord_guilds,
    guild_access_policies,
    guild_access_policy_members,
    guild_agent_settings,
    guild_config_revisions,
    guild_feature_settings,
    guild_persona_assignments,
    persona_assets,
    persona_revisions,
    personas,
)

CONFIG_NOTIFY_CHANNEL = "bot_config_changed"


@dataclass(frozen=True, slots=True)
class PersonaRevisionRecord:
    revision_id: UUID
    persona_id: UUID
    slug: str
    revision: int
    checksum: str


@dataclass(frozen=True, slots=True)
class BootstrapGuild:
    guild_id: int
    name: str | None
    agent: GuildAgentConfig
    policies: tuple[AccessPolicySnapshot, ...]
    features: tuple[GuildFeatureSnapshot, ...]
    persona_revision_id: UUID


class ConfigurationRepository:
    """Keep all SQL for configuration storage outside application services."""

    def __init__(self, client: PostgresClient, storage: ObjectStorage) -> None:
        self.client = client
        self.storage = storage

    async def enabled_guild_ids(self) -> tuple[int, ...]:
        rows = await self.client.fetch_all(
            sa.select(discord_guilds.c.discord_id)
            .where(discord_guilds.c.enabled.is_(True))
            .order_by(discord_guilds.c.discord_id)
        )
        return tuple(int(row["discord_id"]) for row in rows)

    async def draft_revision_id(self, guild_id: int) -> UUID | None:
        row = await self.client.fetch_one(
            sa.select(guild_config_revisions.c.id)
            .where(
                guild_config_revisions.c.guild_id == guild_id,
                guild_config_revisions.c.status == "draft",
            )
            .order_by(guild_config_revisions.c.revision.desc())
            .limit(1)
        )
        return UUID(str(row["id"])) if row is not None else None

    async def create_draft(
        self,
        guild_id: int,
        *,
        actor_user_id: int,
        interaction_id: int,
    ) -> UUID:
        """Clone the active immutable aggregate into one mutable draft revision."""

        async with self.client.transaction() as tx:
            await self._ensure_interaction_is_new(interaction_id, tx)
            guild = (
                (
                    await tx.execute(
                        sa.select(discord_guilds.c.active_config_revision_id)
                        .where(discord_guilds.c.discord_id == guild_id)
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if guild is None or guild["active_config_revision_id"] is None:
                raise LookupError(f"Guild {guild_id} has no active configuration")
            existing = await tx.scalar(
                sa.select(guild_config_revisions.c.id).where(
                    guild_config_revisions.c.guild_id == guild_id,
                    guild_config_revisions.c.status == "draft",
                )
            )
            if existing is not None:
                raise ValueError("The guild already has a configuration draft")
            source_id = UUID(str(guild["active_config_revision_id"]))
            next_revision = int(
                await tx.scalar(
                    sa.select(sa.func.coalesce(sa.func.max(guild_config_revisions.c.revision), 0) + 1).where(
                        guild_config_revisions.c.guild_id == guild_id
                    )
                )
                or 1
            )
            draft_id = uuid.uuid4()
            await tx.execute(
                guild_config_revisions.insert().values(
                    id=draft_id,
                    guild_id=guild_id,
                    revision=next_revision,
                    status="draft",
                    created_by=actor_user_id,
                )
            )
            await self._copy_revision_children(source_id, draft_id, tx)
            await self.audit(
                "config.draft.create",
                guild_id=guild_id,
                actor_user_id=actor_user_id,
                config_revision_id=draft_id,
                interaction_id=interaction_id,
                connection=tx,
            )
            return draft_id

    async def discard_draft(
        self,
        guild_id: int,
        *,
        actor_user_id: int,
        interaction_id: int,
    ) -> None:
        async with self.client.transaction() as tx:
            await self._ensure_interaction_is_new(interaction_id, tx)
            draft_id = await self._draft_id_for_update(guild_id, tx)
            await self.audit(
                "config.draft.discard",
                guild_id=guild_id,
                actor_user_id=actor_user_id,
                config_revision_id=None,
                details={"discarded_revision_id": str(draft_id)},
                interaction_id=interaction_id,
                connection=tx,
            )
            await tx.execute(guild_config_revisions.delete().where(guild_config_revisions.c.id == draft_id))

    async def update_draft_policy(
        self,
        guild_id: int,
        policy: AccessPolicySnapshot,
        *,
        actor_user_id: int,
        interaction_id: int,
    ) -> UUID:
        async with self.client.transaction() as tx:
            await self._ensure_interaction_is_new(interaction_id, tx)
            draft_id = await self._draft_id_for_update(guild_id, tx)
            policy_id = await tx.scalar(
                sa.select(guild_access_policies.c.id).where(
                    guild_access_policies.c.config_revision_id == draft_id,
                    guild_access_policies.c.policy_key == policy.key.value,
                )
            )
            if policy_id is None:
                raise LookupError(f"Draft policy {policy.key.value} is missing")
            await tx.execute(
                guild_access_policies.update()
                .where(guild_access_policies.c.id == policy_id)
                .values(mode=policy.mode.value, subject_type=policy.subject_type)
            )
            await tx.execute(
                guild_access_policy_members.delete().where(
                    guild_access_policy_members.c.policy_id == policy_id
                )
            )
            if policy.ids:
                await tx.execute(
                    guild_access_policy_members.insert(),
                    [
                        {
                            "id": uuid.uuid4(),
                            "policy_id": policy_id,
                            "snowflake_id": snowflake,
                            "position": position,
                        }
                        for position, snowflake in enumerate(policy.ids)
                    ],
                )
            await self.audit(
                "config.access.update",
                guild_id=guild_id,
                actor_user_id=actor_user_id,
                config_revision_id=draft_id,
                details={
                    "policy": policy.key.value,
                    "mode": policy.mode.value,
                    "member_count": len(policy.ids),
                },
                interaction_id=interaction_id,
                connection=tx,
            )
            return draft_id

    async def update_draft_agent(
        self,
        guild_id: int,
        agent: GuildAgentConfig,
        *,
        actor_user_id: int,
        interaction_id: int,
    ) -> UUID:
        async with self.client.transaction() as tx:
            await self._ensure_interaction_is_new(interaction_id, tx)
            draft_id = await self._draft_id_for_update(guild_id, tx)
            await tx.execute(
                guild_agent_settings.update()
                .where(guild_agent_settings.c.config_revision_id == draft_id)
                .values(**agent.model_dump())
            )
            await self.audit(
                "config.agent.update",
                guild_id=guild_id,
                actor_user_id=actor_user_id,
                config_revision_id=draft_id,
                details={"model": agent.model, "reasoning_effort": agent.reasoning_effort},
                interaction_id=interaction_id,
                connection=tx,
            )
            return draft_id

    async def update_draft_feature(
        self,
        guild_id: int,
        feature: GuildFeatureSnapshot,
        *,
        actor_user_id: int,
        interaction_id: int,
    ) -> UUID:
        async with self.client.transaction() as tx:
            await self._ensure_interaction_is_new(interaction_id, tx)
            draft_id = await self._draft_id_for_update(guild_id, tx)
            await tx.execute(
                pg_insert(guild_feature_settings)
                .values(
                    id=uuid.uuid4(),
                    config_revision_id=draft_id,
                    feature_key=feature.key,
                    enabled=feature.enabled,
                    schema_version=feature.schema_version,
                    payload=feature.payload,
                )
                .on_conflict_do_update(
                    constraint="uq_config_feature",
                    set_={
                        "enabled": feature.enabled,
                        "schema_version": feature.schema_version,
                        "payload": feature.payload,
                    },
                )
            )
            await self.audit(
                "config.feature.update",
                guild_id=guild_id,
                actor_user_id=actor_user_id,
                config_revision_id=draft_id,
                details={"feature": feature.key, "enabled": feature.enabled},
                interaction_id=interaction_id,
                connection=tx,
            )
            return draft_id

    async def assign_draft_persona(
        self,
        guild_id: int,
        slug: str,
        revision: int | None,
        *,
        actor_user_id: int,
        interaction_id: int,
    ) -> UUID:
        async with self.client.transaction() as tx:
            await self._ensure_interaction_is_new(interaction_id, tx)
            draft_id = await self._draft_id_for_update(guild_id, tx)
            query = (
                sa.select(persona_revisions.c.id)
                .select_from(
                    persona_revisions.join(personas, persona_revisions.c.persona_id == personas.c.id)
                )
                .where(
                    personas.c.slug == slug,
                    persona_revisions.c.status == "published",
                )
                .order_by(persona_revisions.c.revision.desc())
                .limit(1)
            )
            if revision is not None:
                query = query.where(persona_revisions.c.revision == revision)
            persona_revision_id = await tx.scalar(query)
            if persona_revision_id is None:
                raise LookupError(f"Published persona {slug} revision {revision or 'latest'} not found")
            await tx.execute(
                guild_persona_assignments.update()
                .where(guild_persona_assignments.c.config_revision_id == draft_id)
                .values(persona_revision_id=persona_revision_id)
            )
            await self.audit(
                "config.persona.assign",
                guild_id=guild_id,
                actor_user_id=actor_user_id,
                config_revision_id=draft_id,
                details={"persona": slug, "revision": revision},
                interaction_id=interaction_id,
                connection=tx,
            )
            return draft_id

    async def publish_draft(
        self,
        guild_id: int,
        *,
        actor_user_id: int,
        interaction_id: int,
    ) -> UUID:
        async with self.client.transaction() as tx:
            await self._ensure_interaction_is_new(interaction_id, tx)
            draft_id = await self._draft_id_for_update(guild_id, tx)
            # Validate the exact locked aggregate on this transaction. Draft
            # writers lock the same row, so no mutation can slip between
            # validation and publication.
            await self.snapshot_for_revision(guild_id, draft_id, connection=tx)
            await tx.execute(
                guild_config_revisions.update()
                .where(
                    guild_config_revisions.c.guild_id == guild_id,
                    guild_config_revisions.c.status == "published",
                )
                .values(status="archived")
            )
            await tx.execute(
                guild_config_revisions.update()
                .where(guild_config_revisions.c.id == draft_id)
                .values(status="published", published_at=datetime.now(UTC))
            )
            await tx.execute(
                discord_guilds.update()
                .where(discord_guilds.c.discord_id == guild_id)
                .values(active_config_revision_id=draft_id, updated_at=sa.func.now())
            )
            await self.audit(
                "config.publish",
                guild_id=guild_id,
                actor_user_id=actor_user_id,
                config_revision_id=draft_id,
                interaction_id=interaction_id,
                connection=tx,
            )
            await self.client.notify(
                CONFIG_NOTIFY_CHANNEL,
                json.dumps({"guild_id": guild_id, "revision_id": str(draft_id)}),
                tx,
            )
        return draft_id

    async def rollback(
        self,
        guild_id: int,
        target_revision: int,
        *,
        actor_user_id: int,
        interaction_id: int,
    ) -> UUID:
        target = await self.client.fetch_one(
            sa.select(guild_config_revisions.c.id).where(
                guild_config_revisions.c.guild_id == guild_id,
                guild_config_revisions.c.revision == target_revision,
                guild_config_revisions.c.status.in_(("archived", "published")),
            )
        )
        if target is None:
            raise LookupError(f"Configuration revision {target_revision} was not found")
        source_id = UUID(str(target["id"]))
        async with self.client.transaction() as tx:
            await self._ensure_interaction_is_new(interaction_id, tx)
            await tx.execute(
                sa.select(discord_guilds.c.discord_id)
                .where(discord_guilds.c.discord_id == guild_id)
                .with_for_update()
            )
            if (
                await tx.scalar(
                    sa.select(guild_config_revisions.c.id).where(
                        guild_config_revisions.c.guild_id == guild_id,
                        guild_config_revisions.c.status == "draft",
                    )
                )
                is not None
            ):
                raise ValueError("Discard the current draft before rolling back")
            next_revision = int(
                await tx.scalar(
                    sa.select(sa.func.coalesce(sa.func.max(guild_config_revisions.c.revision), 0) + 1).where(
                        guild_config_revisions.c.guild_id == guild_id
                    )
                )
                or 1
            )
            revision_id = uuid.uuid4()
            await tx.execute(
                guild_config_revisions.update()
                .where(
                    guild_config_revisions.c.guild_id == guild_id,
                    guild_config_revisions.c.status == "published",
                )
                .values(status="archived")
            )
            await tx.execute(
                guild_config_revisions.insert().values(
                    id=revision_id,
                    guild_id=guild_id,
                    revision=next_revision,
                    status="published",
                    created_by=actor_user_id,
                    published_at=datetime.now(UTC),
                )
            )
            await self._copy_revision_children(source_id, revision_id, tx)
            await tx.execute(
                discord_guilds.update()
                .where(discord_guilds.c.discord_id == guild_id)
                .values(active_config_revision_id=revision_id, updated_at=sa.func.now())
            )
            await self.audit(
                "config.rollback",
                guild_id=guild_id,
                actor_user_id=actor_user_id,
                config_revision_id=revision_id,
                details={"source_revision": target_revision},
                interaction_id=interaction_id,
                connection=tx,
            )
            await self.client.notify(
                CONFIG_NOTIFY_CHANNEL,
                json.dumps({"guild_id": guild_id, "revision_id": str(revision_id)}),
                tx,
            )
            return revision_id

    async def active_snapshot(self, guild_id: int) -> GuildConfigSnapshot:
        guild_row = await self.client.fetch_one(
            sa.select(
                discord_guilds.c.active_config_revision_id,
                guild_config_revisions.c.revision,
                guild_persona_assignments.c.persona_revision_id,
                personas.c.slug.label("persona_slug"),
                persona_revisions.c.revision.label("persona_revision"),
            )
            .select_from(
                discord_guilds.join(
                    guild_config_revisions,
                    discord_guilds.c.active_config_revision_id == guild_config_revisions.c.id,
                )
                .join(
                    guild_persona_assignments,
                    guild_config_revisions.c.id == guild_persona_assignments.c.config_revision_id,
                )
                .join(
                    persona_revisions,
                    guild_persona_assignments.c.persona_revision_id == persona_revisions.c.id,
                )
                .join(personas, persona_revisions.c.persona_id == personas.c.id)
            )
            .where(discord_guilds.c.discord_id == guild_id)
            .where(discord_guilds.c.enabled.is_(True))
        )
        if guild_row is None:
            raise LookupError(f"Guild {guild_id} has no active database configuration")
        revision_id = UUID(str(guild_row["active_config_revision_id"]))
        agent_row = await self.client.fetch_one(
            sa.select(guild_agent_settings).where(guild_agent_settings.c.config_revision_id == revision_id)
        )
        if agent_row is None:
            raise LookupError(f"Guild {guild_id} has no agent settings")
        policy_rows = await self.client.fetch_all(
            sa.select(
                guild_access_policies.c.id,
                guild_access_policies.c.policy_key,
                guild_access_policies.c.subject_type,
                guild_access_policies.c.mode,
                guild_access_policy_members.c.snowflake_id,
                guild_access_policy_members.c.position,
            )
            .select_from(
                guild_access_policies.outerjoin(
                    guild_access_policy_members,
                    guild_access_policies.c.id == guild_access_policy_members.c.policy_id,
                )
            )
            .where(guild_access_policies.c.config_revision_id == revision_id)
            .order_by(
                guild_access_policies.c.policy_key,
                guild_access_policy_members.c.position,
                guild_access_policy_members.c.snowflake_id,
            )
        )
        policy_groups: dict[str, dict[str, Any]] = {}
        for row in policy_rows:
            group = policy_groups.setdefault(
                str(row["policy_key"]),
                {
                    "key": row["policy_key"],
                    "subject_type": row["subject_type"],
                    "mode": row["mode"],
                    "ids": [],
                },
            )
            if row["snowflake_id"] is not None:
                group["ids"].append(int(row["snowflake_id"]))
        policies = {
            AccessPolicyKey(key): AccessPolicySnapshot.model_validate(value)
            for key, value in policy_groups.items()
        }
        feature_rows = await self.client.fetch_all(
            sa.select(guild_feature_settings).where(
                guild_feature_settings.c.config_revision_id == revision_id
            )
        )
        features = {
            str(row["feature_key"]): GuildFeatureSnapshot(
                key=str(row["feature_key"]),
                enabled=bool(row["enabled"]),
                schema_version=int(row["schema_version"]),
                payload=dict(row["payload"] or {}),
            )
            for row in feature_rows
        }
        agent_payload = {key: agent_row[key] for key in GuildAgentConfig.model_fields}
        return GuildConfigSnapshot(
            guild_id=guild_id,
            revision_id=revision_id,
            revision=int(guild_row["revision"]),
            agent=GuildAgentConfig.model_validate(agent_payload),
            policies=policies,
            features=features,
            persona_revision_id=UUID(str(guild_row["persona_revision_id"])),
            persona_id=str(guild_row["persona_slug"]),
            persona_revision=int(guild_row["persona_revision"]),
        )

    async def snapshot_for_revision(
        self,
        guild_id: int,
        revision_id: UUID,
        *,
        connection: AsyncConnection | None = None,
    ) -> GuildConfigSnapshot:
        revision_row = await self.client.fetch_one(
            sa.select(
                guild_config_revisions.c.revision,
                guild_persona_assignments.c.persona_revision_id,
                personas.c.slug.label("persona_slug"),
                persona_revisions.c.revision.label("persona_revision"),
            )
            .select_from(
                guild_config_revisions.join(
                    guild_persona_assignments,
                    guild_config_revisions.c.id == guild_persona_assignments.c.config_revision_id,
                )
                .join(
                    persona_revisions,
                    guild_persona_assignments.c.persona_revision_id == persona_revisions.c.id,
                )
                .join(personas, persona_revisions.c.persona_id == personas.c.id)
            )
            .where(
                guild_config_revisions.c.id == revision_id,
                guild_config_revisions.c.guild_id == guild_id,
            ),
            connection=connection,
        )
        if revision_row is None:
            raise LookupError(f"Configuration revision {revision_id} was not found")
        agent_row = await self.client.fetch_one(
            sa.select(guild_agent_settings).where(guild_agent_settings.c.config_revision_id == revision_id),
            connection=connection,
        )
        if agent_row is None:
            raise LookupError("Configuration revision has no agent settings")
        policy_rows = await self.client.fetch_all(
            sa.select(
                guild_access_policies.c.policy_key,
                guild_access_policies.c.subject_type,
                guild_access_policies.c.mode,
                guild_access_policy_members.c.snowflake_id,
            )
            .select_from(
                guild_access_policies.outerjoin(
                    guild_access_policy_members,
                    guild_access_policies.c.id == guild_access_policy_members.c.policy_id,
                )
            )
            .where(guild_access_policies.c.config_revision_id == revision_id)
            .order_by(
                guild_access_policies.c.policy_key,
                guild_access_policy_members.c.position,
            ),
            connection=connection,
        )
        grouped: dict[str, dict[str, Any]] = {}
        for row in policy_rows:
            value = grouped.setdefault(
                str(row["policy_key"]),
                {
                    "key": row["policy_key"],
                    "subject_type": row["subject_type"],
                    "mode": row["mode"],
                    "ids": [],
                },
            )
            if row["snowflake_id"] is not None:
                value["ids"].append(int(row["snowflake_id"]))
        policies = {
            AccessPolicyKey(key): AccessPolicySnapshot.model_validate(value) for key, value in grouped.items()
        }
        feature_rows = await self.client.fetch_all(
            sa.select(guild_feature_settings).where(
                guild_feature_settings.c.config_revision_id == revision_id
            ),
            connection=connection,
        )
        features = {
            str(row["feature_key"]): GuildFeatureSnapshot(
                key=str(row["feature_key"]),
                enabled=bool(row["enabled"]),
                schema_version=int(row["schema_version"]),
                payload=dict(row["payload"] or {}),
            )
            for row in feature_rows
        }
        return GuildConfigSnapshot(
            guild_id=guild_id,
            revision_id=revision_id,
            revision=int(revision_row["revision"]),
            agent=GuildAgentConfig.model_validate(
                {key: agent_row[key] for key in GuildAgentConfig.model_fields}
            ),
            policies=policies,
            features=features,
            persona_revision_id=UUID(str(revision_row["persona_revision_id"])),
            persona_id=str(revision_row["persona_slug"]),
            persona_revision=int(revision_row["persona_revision"]),
        )

    async def load_persona_revision(self, revision_id: UUID) -> LoadedPersona:
        row = await self.client.fetch_one(
            sa.select(
                persona_revisions.c.payload,
                persona_assets.c.object_key,
                persona_assets.c.sha256,
            )
            .select_from(
                persona_revisions.join(
                    persona_assets,
                    sa.and_(
                        persona_revisions.c.id == persona_assets.c.persona_revision_id,
                        persona_assets.c.kind == "avatar",
                    ),
                )
            )
            .where(persona_revisions.c.id == revision_id)
            .where(persona_revisions.c.status == "published")
        )
        if row is None:
            raise PersonaConfigurationError(f"unknown published persona revision {revision_id}")
        profile = BotPersona.model_validate(row["payload"])
        avatar = await self.storage.get(str(row["object_key"]))
        checksum = hashlib.sha256(avatar).hexdigest()
        if checksum != row["sha256"]:
            raise PersonaConfigurationError(f"avatar checksum mismatch for persona revision {revision_id}")
        return LoadedPersona(profile=profile, avatar_bytes=avatar)

    async def global_persona(self) -> LoadedPersona:
        row = await self.client.fetch_one(
            sa.select(bot_application_profile.c.persona_revision_id).where(
                bot_application_profile.c.singleton_id == 1
            )
        )
        if row is None:
            raise PersonaConfigurationError("global bot application profile is not configured")
        return await self.load_persona_revision(UUID(str(row["persona_revision_id"])))

    async def upsert_persona_revision(
        self,
        profile: BotPersona,
        avatar: bytes,
        *,
        content_type: str,
        created_by: int | None = None,
        status: str = "published",
        connection: AsyncConnection | None = None,
    ) -> PersonaRevisionRecord:
        if status not in {"draft", "published"}:
            raise ValueError("Persona revision status must be draft or published")
        payload = profile.model_dump(mode="json")
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        checksum = hashlib.sha256(serialized).hexdigest()
        avatar_checksum = hashlib.sha256(avatar).hexdigest()
        extension = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif"}.get(content_type)
        if extension is None:
            raise PersonaConfigurationError(f"unsupported persona avatar type {content_type}")
        object_key = f"personas/{profile.id}/{avatar_checksum}.{extension}"
        if not await self.storage.exists(object_key):
            await self.storage.put(object_key, avatar, content_type)

        async def write(tx: AsyncConnection) -> PersonaRevisionRecord:
            persona_id = uuid.uuid4()
            result = await tx.execute(
                pg_insert(personas)
                .values(id=persona_id, slug=profile.id)
                .on_conflict_do_update(
                    index_elements=[personas.c.slug],
                    set_={"updated_at": sa.func.now()},
                )
                .returning(personas.c.id)
            )
            resolved_persona_id = UUID(str(result.scalar_one()))
            existing = (
                (
                    await tx.execute(
                        sa.select(
                            persona_revisions.c.id,
                            persona_revisions.c.revision,
                            persona_revisions.c.checksum,
                        ).where(
                            persona_revisions.c.persona_id == resolved_persona_id,
                            persona_revisions.c.checksum == checksum,
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
            if existing is not None:
                if status == "published":
                    await tx.execute(
                        persona_revisions.update()
                        .where(persona_revisions.c.id == existing["id"])
                        .where(persona_revisions.c.status == "draft")
                        .values(status="published", published_at=datetime.now(UTC))
                    )
                return PersonaRevisionRecord(
                    revision_id=UUID(str(existing["id"])),
                    persona_id=resolved_persona_id,
                    slug=profile.id,
                    revision=int(existing["revision"]),
                    checksum=str(existing["checksum"]),
                )
            revision_id = uuid.uuid4()
            conflicting_revision = await tx.scalar(
                sa.select(persona_revisions.c.id).where(
                    persona_revisions.c.persona_id == resolved_persona_id,
                    persona_revisions.c.revision == profile.revision,
                )
            )
            if conflicting_revision is not None:
                raise PersonaConfigurationError(
                    f"persona {profile.id} revision {profile.revision} already exists with different content"
                )
            await tx.execute(
                persona_revisions.insert().values(
                    id=revision_id,
                    persona_id=resolved_persona_id,
                    revision=profile.revision,
                    schema_version=profile.schema_version,
                    status=status,
                    payload=payload,
                    checksum=checksum,
                    created_by=created_by,
                    published_at=datetime.now(UTC) if status == "published" else None,
                )
            )
            await tx.execute(
                persona_assets.insert().values(
                    id=uuid.uuid4(),
                    persona_revision_id=revision_id,
                    kind="avatar",
                    object_key=object_key,
                    sha256=avatar_checksum,
                    content_type=content_type,
                    size_bytes=len(avatar),
                )
            )
            return PersonaRevisionRecord(
                revision_id=revision_id,
                persona_id=resolved_persona_id,
                slug=profile.id,
                revision=profile.revision,
                checksum=checksum,
            )

        if connection is not None:
            return await write(connection)
        async with self.client.transaction() as transaction:
            return await write(transaction)

    async def import_persona_draft(
        self,
        profile: BotPersona,
        avatar: bytes,
        *,
        content_type: str,
        actor_user_id: int,
        interaction_id: int,
    ) -> PersonaRevisionRecord:
        """Store a persona draft and its audit event atomically."""

        async with self.client.transaction() as tx:
            await self._ensure_interaction_is_new(interaction_id, tx)
            record = await self.upsert_persona_revision(
                profile,
                avatar,
                content_type=content_type,
                created_by=actor_user_id,
                status="draft",
                connection=tx,
            )
            await self.audit(
                "config.persona.import",
                guild_id=None,
                actor_user_id=actor_user_id,
                config_revision_id=None,
                details={"persona": record.slug, "revision": record.revision},
                interaction_id=interaction_id,
                connection=tx,
            )
            return record

    async def record_global_profile_sync(
        self,
        *,
        synchronized: bool,
        error: str | None = None,
    ) -> None:
        """Persist the latest Discord global-profile synchronization result."""

        values: dict[str, Any] = {
            "sync_status": "synced" if synchronized else "failed",
            "last_error": None if synchronized else (error or "Discord profile synchronization failed")[:500],
            "updated_at": sa.func.now(),
        }
        if synchronized:
            values["last_synced_at"] = datetime.now(UTC)
        await self.client.execute(
            bot_application_profile.update()
            .where(bot_application_profile.c.singleton_id == 1)
            .values(**values)
        )

    async def publish_persona_revision(
        self,
        slug: str,
        revision: int,
        *,
        actor_user_id: int,
        interaction_id: int,
    ) -> UUID:
        async with self.client.transaction() as tx:
            await self._ensure_interaction_is_new(interaction_id, tx)
            row = (
                (
                    await tx.execute(
                        sa.select(persona_revisions.c.id, persona_revisions.c.status)
                        .select_from(
                            persona_revisions.join(
                                personas,
                                persona_revisions.c.persona_id == personas.c.id,
                            )
                        )
                        .where(
                            personas.c.slug == slug,
                            persona_revisions.c.revision == revision,
                        )
                        .with_for_update()
                    )
                )
                .mappings()
                .one_or_none()
            )
            if row is None:
                raise LookupError(f"Persona {slug} revision {revision} was not found")
            revision_id = UUID(str(row["id"]))
            if row["status"] == "archived":
                raise ValueError("An archived persona revision cannot be published")
            await tx.execute(
                persona_revisions.update()
                .where(persona_revisions.c.id == revision_id)
                .values(status="published", published_at=datetime.now(UTC))
            )
            await self.audit(
                "config.persona.publish",
                guild_id=None,
                actor_user_id=actor_user_id,
                config_revision_id=None,
                details={"persona": slug, "revision": revision},
                interaction_id=interaction_id,
                connection=tx,
            )
            return revision_id

    async def bootstrap_guild(
        self,
        item: BootstrapGuild,
        *,
        connection: AsyncConnection,
    ) -> UUID:
        await connection.execute(
            pg_insert(discord_guilds)
            .values(discord_id=item.guild_id, name=item.name, enabled=True)
            .on_conflict_do_update(
                index_elements=[discord_guilds.c.discord_id],
                set_={"name": item.name, "enabled": True, "updated_at": sa.func.now()},
            )
        )
        await connection.execute(
            sa.select(discord_guilds.c.discord_id)
            .where(discord_guilds.c.discord_id == item.guild_id)
            .with_for_update()
        )
        revision_id = uuid.uuid4()
        next_revision = await connection.scalar(
            sa.select(sa.func.coalesce(sa.func.max(guild_config_revisions.c.revision), 0) + 1).where(
                guild_config_revisions.c.guild_id == item.guild_id
            )
        )
        await connection.execute(
            guild_config_revisions.update()
            .where(
                guild_config_revisions.c.guild_id == item.guild_id,
                guild_config_revisions.c.status == "published",
            )
            .values(status="archived")
        )
        await connection.execute(
            guild_config_revisions.insert().values(
                id=revision_id,
                guild_id=item.guild_id,
                revision=int(next_revision or 1),
                status="published",
                published_at=datetime.now(UTC),
            )
        )
        await connection.execute(
            guild_agent_settings.insert().values(
                config_revision_id=revision_id,
                **item.agent.model_dump(),
            )
        )
        for policy in item.policies:
            policy_id = uuid.uuid4()
            await connection.execute(
                guild_access_policies.insert().values(
                    id=policy_id,
                    config_revision_id=revision_id,
                    policy_key=policy.key.value,
                    subject_type=policy.subject_type,
                    mode=policy.mode.value,
                )
            )
            if policy.ids:
                await connection.execute(
                    guild_access_policy_members.insert(),
                    [
                        {
                            "id": uuid.uuid4(),
                            "policy_id": policy_id,
                            "snowflake_id": snowflake,
                            "position": position,
                        }
                        for position, snowflake in enumerate(policy.ids)
                    ],
                )
        if item.features:
            await connection.execute(
                guild_feature_settings.insert(),
                [
                    {
                        "id": uuid.uuid4(),
                        "config_revision_id": revision_id,
                        "feature_key": feature.key,
                        "enabled": feature.enabled,
                        "schema_version": feature.schema_version,
                        "payload": feature.payload,
                    }
                    for feature in item.features
                ],
            )
        await connection.execute(
            guild_persona_assignments.insert().values(
                config_revision_id=revision_id,
                persona_revision_id=item.persona_revision_id,
            )
        )
        await connection.execute(
            discord_guilds.update()
            .where(discord_guilds.c.discord_id == item.guild_id)
            .values(active_config_revision_id=revision_id, updated_at=sa.func.now())
        )
        return revision_id

    async def set_global_persona(
        self,
        persona_revision_id: UUID,
        *,
        actor_user_id: int | None = None,
        connection: AsyncConnection,
    ) -> None:
        await connection.execute(
            pg_insert(bot_application_profile)
            .values(
                singleton_id=1,
                persona_revision_id=persona_revision_id,
                sync_status="pending",
                updated_by=actor_user_id,
            )
            .on_conflict_do_update(
                index_elements=[bot_application_profile.c.singleton_id],
                set_={
                    "persona_revision_id": persona_revision_id,
                    "sync_status": "pending",
                    "last_error": None,
                    "updated_by": actor_user_id,
                    "updated_at": sa.func.now(),
                },
            )
        )

    async def set_global_persona_by_slug(
        self,
        slug: str,
        revision: int | None,
        *,
        actor_user_id: int,
        interaction_id: int,
    ) -> UUID:
        async with self.client.transaction() as tx:
            await self._ensure_interaction_is_new(interaction_id, tx)
            query = (
                sa.select(persona_revisions.c.id)
                .select_from(
                    persona_revisions.join(personas, persona_revisions.c.persona_id == personas.c.id)
                )
                .where(
                    personas.c.slug == slug,
                    persona_revisions.c.status == "published",
                )
                .order_by(persona_revisions.c.revision.desc())
                .limit(1)
            )
            if revision is not None:
                query = query.where(persona_revisions.c.revision == revision)
            revision_id = await tx.scalar(query)
            if revision_id is None:
                raise LookupError(f"Published persona {slug} revision {revision or 'latest'} not found")
            resolved_id = UUID(str(revision_id))
            await self.set_global_persona(
                resolved_id,
                actor_user_id=actor_user_id,
                connection=tx,
            )
            await self.audit(
                "config.global_profile.update",
                guild_id=None,
                actor_user_id=actor_user_id,
                config_revision_id=None,
                details={"persona": slug, "revision": revision},
                interaction_id=interaction_id,
                connection=tx,
            )
            await self.client.notify(
                CONFIG_NOTIFY_CHANNEL,
                json.dumps({"global_profile": True, "persona_revision_id": str(resolved_id)}),
                tx,
            )
            return resolved_id

    async def migration_record(self, name: str) -> sa.RowMapping | None:
        return await self.client.fetch_one(sa.select(data_migrations).where(data_migrations.c.name == name))

    async def record_migration(
        self,
        name: str,
        checksum: str,
        details: dict[str, Any],
        *,
        connection: AsyncConnection,
    ) -> None:
        await connection.execute(
            pg_insert(data_migrations)
            .values(name=name, checksum=checksum, details=details)
            .on_conflict_do_update(
                index_elements=[data_migrations.c.name],
                set_={
                    "checksum": checksum,
                    "details": details,
                    "executed_at": sa.func.now(),
                },
            )
        )

    async def audit(
        self,
        operation: str,
        *,
        guild_id: int | None,
        actor_user_id: int | None,
        config_revision_id: UUID | None,
        details: dict[str, Any] | None = None,
        interaction_id: int | None = None,
        connection: AsyncConnection,
    ) -> None:
        await connection.execute(
            config_audit_events.insert().values(
                id=uuid.uuid4(),
                guild_id=guild_id,
                actor_user_id=actor_user_id,
                discord_interaction_id=interaction_id,
                operation=operation,
                config_revision_id=config_revision_id,
                details=details or {},
            )
        )

    async def verify(self) -> dict[str, Any]:
        guild_ids = await self.enabled_guild_ids()
        snapshots = [await self.active_snapshot(guild_id) for guild_id in guild_ids]
        global_persona = await self.global_persona()
        missing_assets: list[str] = []
        asset_rows = await self.client.fetch_all(sa.select(persona_assets.c.object_key))
        for row in asset_rows:
            key = str(row["object_key"])
            if not await self.storage.exists(key):
                missing_assets.append(key)
        return {
            "ok": bool(guild_ids) and not missing_assets,
            "guild_count": len(guild_ids),
            "guild_revisions": {str(item.guild_id): item.revision for item in snapshots},
            "global_persona": global_persona.profile.id,
            "global_persona_revision": global_persona.profile.revision,
            "asset_count": len(asset_rows),
            "missing_assets": missing_assets,
        }

    async def _draft_id_for_update(
        self,
        guild_id: int,
        connection: AsyncConnection,
    ) -> UUID:
        value = await connection.scalar(
            sa.select(guild_config_revisions.c.id)
            .where(
                guild_config_revisions.c.guild_id == guild_id,
                guild_config_revisions.c.status == "draft",
            )
            .with_for_update()
        )
        if value is None:
            raise LookupError("Create a configuration draft first")
        return UUID(str(value))

    async def _ensure_interaction_is_new(
        self,
        interaction_id: int,
        connection: AsyncConnection,
    ) -> None:
        existing = await connection.scalar(
            sa.select(config_audit_events.c.id).where(
                config_audit_events.c.discord_interaction_id == interaction_id
            )
        )
        if existing is not None:
            raise ValueError("This Discord interaction was already applied")

    async def _copy_revision_children(
        self,
        source_id: UUID,
        target_id: UUID,
        connection: AsyncConnection,
    ) -> None:
        agent_columns = list(GuildAgentConfig.model_fields)
        await connection.execute(
            guild_agent_settings.insert().from_select(
                ["config_revision_id", *agent_columns],
                sa.select(
                    sa.literal(target_id), *[guild_agent_settings.c[name] for name in agent_columns]
                ).where(guild_agent_settings.c.config_revision_id == source_id),
            )
        )
        source_policies = (
            (
                await connection.execute(
                    sa.select(guild_access_policies).where(
                        guild_access_policies.c.config_revision_id == source_id
                    )
                )
            )
            .mappings()
            .all()
        )
        for source_policy in source_policies:
            policy_id = uuid.uuid4()
            await connection.execute(
                guild_access_policies.insert().values(
                    id=policy_id,
                    config_revision_id=target_id,
                    policy_key=source_policy["policy_key"],
                    subject_type=source_policy["subject_type"],
                    mode=source_policy["mode"],
                )
            )
            members = (
                (
                    await connection.execute(
                        sa.select(
                            guild_access_policy_members.c.snowflake_id,
                            guild_access_policy_members.c.position,
                        ).where(guild_access_policy_members.c.policy_id == source_policy["id"])
                    )
                )
                .mappings()
                .all()
            )
            if members:
                await connection.execute(
                    guild_access_policy_members.insert(),
                    [
                        {
                            "id": uuid.uuid4(),
                            "policy_id": policy_id,
                            "snowflake_id": member["snowflake_id"],
                            "position": member["position"],
                        }
                        for member in members
                    ],
                )
        source_features = (
            (
                await connection.execute(
                    sa.select(
                        guild_feature_settings.c.feature_key,
                        guild_feature_settings.c.enabled,
                        guild_feature_settings.c.schema_version,
                        guild_feature_settings.c.payload,
                    ).where(guild_feature_settings.c.config_revision_id == source_id)
                )
            )
            .mappings()
            .all()
        )
        if source_features:
            await connection.execute(
                guild_feature_settings.insert(),
                [
                    {
                        "id": uuid.uuid4(),
                        "config_revision_id": target_id,
                        "feature_key": feature["feature_key"],
                        "enabled": feature["enabled"],
                        "schema_version": feature["schema_version"],
                        "payload": feature["payload"],
                    }
                    for feature in source_features
                ],
            )
        await connection.execute(
            guild_persona_assignments.insert().from_select(
                ["config_revision_id", "persona_revision_id"],
                sa.select(
                    sa.literal(target_id),
                    guild_persona_assignments.c.persona_revision_id,
                ).where(guild_persona_assignments.c.config_revision_id == source_id),
            )
        )

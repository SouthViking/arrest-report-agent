"""SQLAlchemy Core schema for versioned configuration storage."""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

metadata = sa.MetaData()


def uuid_column(name: str = "id", *, primary_key: bool = False) -> sa.Column[uuid.UUID]:
    return sa.Column(
        name,
        postgresql.UUID(as_uuid=True),
        primary_key=primary_key,
        default=uuid.uuid4,
    )


discord_guilds = sa.Table(
    "discord_guilds",
    metadata,
    sa.Column("discord_id", sa.BigInteger, primary_key=True),
    sa.Column("name", sa.String(100)),
    sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
    sa.Column(
        "active_config_revision_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("guild_config_revisions.id", use_alter=True, ondelete="SET NULL"),
    ),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
)

personas = sa.Table(
    "personas",
    metadata,
    uuid_column(primary_key=True),
    sa.Column("slug", sa.String(80), nullable=False, unique=True),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
)

persona_revisions = sa.Table(
    "persona_revisions",
    metadata,
    uuid_column(primary_key=True),
    sa.Column(
        "persona_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("personas.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("revision", sa.Integer, nullable=False),
    sa.Column("schema_version", sa.Integer, nullable=False),
    sa.Column("status", sa.String(20), nullable=False),
    sa.Column("payload", postgresql.JSONB, nullable=False),
    sa.Column("checksum", sa.String(64), nullable=False),
    sa.Column("created_by", sa.BigInteger),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.Column("published_at", sa.DateTime(timezone=True)),
    sa.CheckConstraint("status IN ('draft','published','archived')", name="ck_persona_revision_status"),
    sa.UniqueConstraint("persona_id", "revision", name="uq_persona_revision"),
    sa.UniqueConstraint("persona_id", "checksum", name="uq_persona_checksum"),
)

persona_assets = sa.Table(
    "persona_assets",
    metadata,
    uuid_column(primary_key=True),
    sa.Column(
        "persona_revision_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("persona_revisions.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("kind", sa.String(30), nullable=False),
    sa.Column("object_key", sa.String(500), nullable=False),
    sa.Column("sha256", sa.String(64), nullable=False),
    sa.Column("content_type", sa.String(100), nullable=False),
    sa.Column("size_bytes", sa.BigInteger, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.UniqueConstraint("persona_revision_id", "kind", name="uq_persona_asset_kind"),
)

guild_config_revisions = sa.Table(
    "guild_config_revisions",
    metadata,
    uuid_column(primary_key=True),
    sa.Column(
        "guild_id",
        sa.BigInteger,
        sa.ForeignKey("discord_guilds.discord_id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("revision", sa.Integer, nullable=False),
    sa.Column("status", sa.String(20), nullable=False),
    sa.Column("created_by", sa.BigInteger),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.Column("published_at", sa.DateTime(timezone=True)),
    sa.CheckConstraint("status IN ('draft','published','archived')", name="ck_guild_config_status"),
    sa.UniqueConstraint("guild_id", "revision", name="uq_guild_config_revision"),
    sa.Index("ix_config_revision_guild_status", "guild_id", "status"),
)

guild_agent_settings = sa.Table(
    "guild_agent_settings",
    metadata,
    sa.Column(
        "config_revision_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("guild_config_revisions.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("model", sa.String(100), nullable=False),
    sa.Column("reasoning_effort", sa.String(30), nullable=False),
    sa.Column("review_timeout_seconds", sa.Float, nullable=False),
    sa.Column("max_agent_turns", sa.Integer, nullable=False),
    sa.Column("max_images_per_review", sa.Integer, nullable=False),
    sa.Column("max_image_bytes", sa.BigInteger, nullable=False),
    sa.Column("max_files_per_review", sa.Integer, nullable=False),
    sa.Column("max_file_bytes", sa.BigInteger, nullable=False),
    sa.Column("max_total_file_bytes", sa.BigInteger, nullable=False),
    sa.Column("thread_context_max_messages", sa.Integer, nullable=False),
    sa.Column("channel_context_max_messages", sa.Integer, nullable=False),
    sa.Column("thread_context_max_chars", sa.Integer, nullable=False),
    sa.Column("thread_context_max_message_chars", sa.Integer, nullable=False),
    sa.Column("thread_context_max_historical_images", sa.Integer, nullable=False),
    sa.Column("thread_context_max_historical_files", sa.Integer, nullable=False),
    sa.Column("thread_auto_archive_minutes", sa.Integer, nullable=False),
)

guild_access_policies = sa.Table(
    "guild_access_policies",
    metadata,
    uuid_column(primary_key=True),
    sa.Column(
        "config_revision_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("guild_config_revisions.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("policy_key", sa.String(80), nullable=False),
    sa.Column("subject_type", sa.String(20), nullable=False),
    sa.Column("mode", sa.String(20), nullable=False),
    sa.CheckConstraint("subject_type IN ('channel','role','user')", name="ck_policy_subject_type"),
    sa.CheckConstraint("mode IN ('open','restricted','disabled')", name="ck_policy_mode"),
    sa.UniqueConstraint("config_revision_id", "policy_key", name="uq_config_policy"),
)

guild_access_policy_members = sa.Table(
    "guild_access_policy_members",
    metadata,
    uuid_column(primary_key=True),
    sa.Column(
        "policy_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("guild_access_policies.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("snowflake_id", sa.BigInteger, nullable=False),
    sa.Column("position", sa.Integer, nullable=False, server_default="0"),
    sa.UniqueConstraint("policy_id", "snowflake_id", name="uq_policy_member"),
)

guild_feature_settings = sa.Table(
    "guild_feature_settings",
    metadata,
    uuid_column(primary_key=True),
    sa.Column(
        "config_revision_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("guild_config_revisions.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("feature_key", sa.String(80), nullable=False),
    sa.Column("enabled", sa.Boolean, nullable=False),
    sa.Column("schema_version", sa.Integer, nullable=False, server_default="1"),
    sa.Column("payload", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.UniqueConstraint("config_revision_id", "feature_key", name="uq_config_feature"),
)

guild_persona_assignments = sa.Table(
    "guild_persona_assignments",
    metadata,
    sa.Column(
        "config_revision_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("guild_config_revisions.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column(
        "persona_revision_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("persona_revisions.id", ondelete="RESTRICT"),
        nullable=False,
    ),
)

bot_application_profile = sa.Table(
    "bot_application_profile",
    metadata,
    sa.Column("singleton_id", sa.SmallInteger, primary_key=True),
    sa.Column(
        "persona_revision_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("persona_revisions.id", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("sync_status", sa.String(20), nullable=False, server_default="pending"),
    sa.Column("last_synced_at", sa.DateTime(timezone=True)),
    sa.Column("last_error", sa.String(500)),
    sa.Column("updated_by", sa.BigInteger),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.CheckConstraint("singleton_id = 1", name="ck_singleton_profile"),
    sa.CheckConstraint("sync_status IN ('pending','synced','failed')", name="ck_profile_sync_status"),
)

config_audit_events = sa.Table(
    "config_audit_events",
    metadata,
    uuid_column(primary_key=True),
    sa.Column("guild_id", sa.BigInteger, sa.ForeignKey("discord_guilds.discord_id", ondelete="SET NULL")),
    sa.Column("actor_user_id", sa.BigInteger),
    sa.Column("discord_interaction_id", sa.BigInteger, unique=True),
    sa.Column("operation", sa.String(100), nullable=False),
    sa.Column(
        "config_revision_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("guild_config_revisions.id", ondelete="SET NULL"),
    ),
    sa.Column("details", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
)

data_migrations = sa.Table(
    "data_migrations",
    metadata,
    sa.Column("name", sa.String(120), primary_key=True),
    sa.Column("checksum", sa.String(64), nullable=False),
    sa.Column("details", postgresql.JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
    sa.Column("executed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
)

guild_member_profiles = sa.Table(
    "guild_member_profiles",
    metadata,
    uuid_column(primary_key=True),
    sa.Column(
        "guild_id",
        sa.BigInteger,
        sa.ForeignKey("discord_guilds.discord_id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("user_id", sa.BigInteger, nullable=False),
    sa.Column("callsign", sa.String(40), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.UniqueConstraint("guild_id", "user_id", name="uq_guild_member_profile"),
    sa.Index("ix_guild_member_profile_callsign", "guild_id", "callsign"),
)

pagers = sa.Table(
    "pagers",
    metadata,
    uuid_column(primary_key=True),
    sa.Column(
        "guild_id",
        sa.BigInteger,
        sa.ForeignKey("discord_guilds.discord_id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("channel_id", sa.BigInteger, nullable=False),
    sa.Column("message_id", sa.BigInteger),
    sa.Column("number", sa.Integer, nullable=False),
    sa.Column("creator_user_id", sa.BigInteger, nullable=False),
    sa.Column("creator_callsign", sa.String(40), nullable=False),
    sa.Column("location", sa.String(200), nullable=False),
    sa.Column("situation", sa.Text, nullable=False),
    sa.Column("status", sa.String(20), nullable=False),
    sa.Column("resolution", sa.String(1000)),
    sa.Column("closed_by_user_id", sa.BigInteger),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.Column("closed_at", sa.DateTime(timezone=True)),
    sa.CheckConstraint(
        "status IN ('publishing','active','concluded','cancelled','failed')",
        name="ck_pager_status",
    ),
    sa.CheckConstraint("number BETWEEN 1 AND 99999", name="ck_pager_number"),
    sa.UniqueConstraint("guild_id", "number", name="uq_pager_guild_number"),
    sa.UniqueConstraint("guild_id", "channel_id", "message_id", name="uq_pager_message"),
    sa.Index("ix_pager_guild_status_created", "guild_id", "status", "created_at"),
    sa.Index("ix_pager_creator", "guild_id", "creator_user_id", "created_at"),
)

pager_recipient_roles = sa.Table(
    "pager_recipient_roles",
    metadata,
    uuid_column(primary_key=True),
    sa.Column(
        "pager_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("pagers.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("role_id", sa.BigInteger, nullable=False),
    sa.Column("position", sa.Integer, nullable=False),
    sa.CheckConstraint("position >= 0", name="ck_pager_recipient_position"),
    sa.UniqueConstraint("pager_id", "role_id", name="uq_pager_recipient_role"),
    sa.UniqueConstraint("pager_id", "position", name="uq_pager_recipient_position"),
)

pager_responders = sa.Table(
    "pager_responders",
    metadata,
    uuid_column(primary_key=True),
    sa.Column(
        "pager_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("pagers.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("user_id", sa.BigInteger, nullable=False),
    sa.Column("callsign", sa.String(40), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.UniqueConstraint("pager_id", "user_id", name="uq_pager_responder"),
    sa.Index("ix_pager_responder_user", "user_id", "updated_at"),
)

deployment_events = sa.Table(
    "deployment_events",
    metadata,
    uuid_column(primary_key=True),
    sa.Column("provider_event_key", sa.String(64), nullable=False, unique=True),
    sa.Column("event_type", sa.String(30), nullable=False),
    sa.Column("deployment_id", sa.String(100), nullable=False),
    sa.Column("project_id", sa.String(100), nullable=False),
    sa.Column("environment_id", sa.String(100), nullable=False),
    sa.Column("service_id", sa.String(100), nullable=False),
    sa.Column("source", sa.String(50)),
    sa.Column("branch", sa.String(255)),
    sa.Column("commit_hash", sa.String(64)),
    sa.Column("commit_message", sa.String(500)),
    sa.Column("severity", sa.String(30)),
    sa.Column("event_timestamp", sa.DateTime(timezone=True), nullable=False),
    sa.Column("status", sa.String(20), nullable=False, server_default="received"),
    sa.Column("ignored_reason", sa.String(100)),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.Column("processed_at", sa.DateTime(timezone=True)),
    sa.CheckConstraint(
        "event_type IN ('deployed','failed','crashed','oom_killed')",
        name="ck_deployment_event_type",
    ),
    sa.CheckConstraint(
        "status IN ('received','processed','ignored')",
        name="ck_deployment_event_status",
    ),
    sa.Index("ix_deployment_event_status_created", "status", "created_at"),
)

deployment_notification_deliveries = sa.Table(
    "deployment_notification_deliveries",
    metadata,
    uuid_column(primary_key=True),
    sa.Column(
        "event_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("deployment_events.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column(
        "guild_id",
        sa.BigInteger,
        sa.ForeignKey("discord_guilds.discord_id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("channel_id", sa.BigInteger, nullable=False),
    sa.Column("notification_kind", sa.String(20), nullable=False),
    sa.Column("notification_key", sa.String(200), nullable=False, unique=True),
    sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
    sa.Column("discord_message_id", sa.BigInteger),
    sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
    sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.Column("last_error", sa.String(500)),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.Column("sent_at", sa.DateTime(timezone=True)),
    sa.CheckConstraint(
        "notification_kind IN ('release','incident')",
        name="ck_deployment_notification_kind",
    ),
    sa.CheckConstraint(
        "status IN ('pending','retry','sent','failed')",
        name="ck_deployment_notification_status",
    ),
    sa.Index(
        "ix_deployment_notification_pending",
        "status",
        "next_attempt_at",
    ),
)

suggestion_counters = sa.Table(
    "suggestion_counters",
    metadata,
    sa.Column(
        "guild_id",
        sa.BigInteger,
        sa.ForeignKey("discord_guilds.discord_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("last_number", sa.Integer, nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.CheckConstraint("last_number > 0", name="ck_suggestion_counter_positive"),
)

suggestions = sa.Table(
    "suggestions",
    metadata,
    uuid_column(primary_key=True),
    sa.Column(
        "guild_id",
        sa.BigInteger,
        sa.ForeignKey("discord_guilds.discord_id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("number", sa.Integer, nullable=False),
    sa.Column("author_user_id", sa.BigInteger, nullable=False),
    sa.Column("source_channel_id", sa.BigInteger, nullable=False),
    sa.Column("category_key", sa.String(30), nullable=False),
    sa.Column("title", sa.String(100), nullable=False),
    sa.Column("description", sa.Text, nullable=False),
    sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
    sa.Column("version", sa.Integer, nullable=False, server_default="1"),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.Column("archived_at", sa.DateTime(timezone=True)),
    sa.CheckConstraint(
        "category_key IN ('automation','procedure','documentation','experience','correction','other')",
        name="ck_suggestion_category",
    ),
    sa.CheckConstraint("status IN ('pending','archived')", name="ck_suggestion_status"),
    sa.CheckConstraint("number > 0", name="ck_suggestion_number_positive"),
    sa.CheckConstraint("version > 0", name="ck_suggestion_version_positive"),
    sa.UniqueConstraint("guild_id", "number", name="uq_suggestion_guild_number"),
    sa.Index("ix_suggestion_guild_created", "guild_id", "created_at"),
    sa.Index(
        "ix_suggestion_author_pending",
        "guild_id",
        "author_user_id",
        "status",
        "created_at",
    ),
)

suggestion_events = sa.Table(
    "suggestion_events",
    metadata,
    uuid_column(primary_key=True),
    sa.Column(
        "suggestion_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("suggestions.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("event_type", sa.String(30), nullable=False),
    sa.Column("actor_user_id", sa.BigInteger, nullable=False),
    sa.Column("from_status", sa.String(20)),
    sa.Column("to_status", sa.String(20), nullable=False),
    sa.Column("comment", sa.Text),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.CheckConstraint(
        "event_type IN ('submitted','status_changed')",
        name="ck_suggestion_event_type",
    ),
    sa.CheckConstraint(
        "from_status IS NULL OR from_status IN ('pending','archived')",
        name="ck_suggestion_event_from_status",
    ),
    sa.CheckConstraint(
        "to_status IN ('pending','archived')",
        name="ck_suggestion_event_to_status",
    ),
    sa.Index("ix_suggestion_event_history", "suggestion_id", "created_at"),
)

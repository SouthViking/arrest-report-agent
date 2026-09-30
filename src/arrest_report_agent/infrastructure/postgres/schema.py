"""SQLAlchemy Core schema for versioned configuration storage."""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

metadata = sa.MetaData()


pending_arrest_digest_state = sa.Table(
    "pending_arrest_digest_state",
    metadata,
    sa.Column(
        "guild_id",
        sa.BigInteger,
        sa.ForeignKey("discord_guilds.discord_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("next_due_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("cycle_id", postgresql.UUID(as_uuid=True)),
    sa.Column("lease_until", sa.DateTime(timezone=True)),
    sa.Column("last_completed_at", sa.DateTime(timezone=True)),
)

pending_arrest_digest_deliveries = sa.Table(
    "pending_arrest_digest_deliveries",
    metadata,
    sa.Column(
        "guild_id",
        sa.BigInteger,
        sa.ForeignKey("discord_guilds.discord_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("cycle_id", postgresql.UUID(as_uuid=True), primary_key=True),
    sa.Column("channel_id", sa.BigInteger, primary_key=True),
    sa.Column("message_id", sa.BigInteger, nullable=False),
    sa.Column("reply_sent", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
)

pending_arrest_digest_mentions = sa.Table(
    "pending_arrest_digest_mentions",
    metadata,
    sa.Column(
        "guild_id",
        sa.BigInteger,
        sa.ForeignKey("discord_guilds.discord_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("report_id", sa.BigInteger, primary_key=True),
    sa.Column("user_id", sa.BigInteger, primary_key=True),
    sa.Column("stage", sa.SmallInteger, nullable=False),
    sa.Column("mentioned_at", sa.DateTime(timezone=True), nullable=False),
    sa.CheckConstraint("stage IN (0,1,2)", name="ck_pending_digest_mention_stage"),
)


rumor_publications = sa.Table(
    "rumor_publications",
    metadata,
    sa.Column("interaction_id", sa.BigInteger, primary_key=True),
    sa.Column(
        "guild_id",
        sa.BigInteger,
        sa.ForeignKey("discord_guilds.discord_id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("channel_id", sa.BigInteger, nullable=False),
    sa.Column("role_id", sa.BigInteger, nullable=False),
    sa.Column("author_user_id", sa.BigInteger, nullable=False),
    sa.Column("message_id", sa.BigInteger),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.UniqueConstraint("guild_id", "channel_id", "message_id", name="uq_rumor_message"),
)
sa.Index(
    "ix_rumor_author",
    rumor_publications.c.guild_id,
    rumor_publications.c.author_user_id,
    rumor_publications.c.created_at,
)


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

agent_sessions = sa.Table(
    "agent_sessions",
    metadata,
    sa.Column("session_id", sa.String(70), primary_key=True),
    sa.Column("namespace", sa.String(80), nullable=False),
    sa.Column("scope_key", sa.String(255), nullable=False),
    sa.Column("access_partition", sa.String(80), nullable=False),
    sa.Column("source_cursor", sa.String(255)),
    sa.Column("created_at", sa.DateTime(timezone=False), nullable=False, server_default=sa.func.now()),
    sa.Column("updated_at", sa.DateTime(timezone=False), nullable=False, server_default=sa.func.now()),
    sa.UniqueConstraint(
        "namespace",
        "scope_key",
        "access_partition",
        name="uq_agent_session_scope",
    ),
)

agent_messages = sa.Table(
    "agent_messages",
    metadata,
    sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
    sa.Column(
        "session_id",
        sa.String(70),
        sa.ForeignKey("agent_sessions.session_id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("message_data", sa.Text, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=False), nullable=False, server_default=sa.func.now()),
    sa.Index("idx_agent_messages_session_time", "session_id", "created_at"),
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

ftp_training_requests = sa.Table(
    "ftp_training_requests",
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
    sa.Column("requester_user_id", sa.BigInteger, nullable=False),
    sa.Column("reason", sa.String(500), nullable=False),
    sa.Column("training_phase", sa.String(20)),
    sa.Column("requested_instructor_level", sa.String(20)),
    sa.Column("scheduled_for", sa.DateTime(timezone=True)),
    sa.Column("status", sa.String(20), nullable=False),
    sa.Column("claimed_by_user_id", sa.BigInteger),
    sa.Column("resolved_by_user_id", sa.BigInteger),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.Column("resolved_at", sa.DateTime(timezone=True)),
    sa.Column("auto_cancelled_at", sa.DateTime(timezone=True)),
    sa.Column("expiry_sync_due_at", sa.DateTime(timezone=True)),
    sa.CheckConstraint(
        "(auto_cancelled_at IS NULL AND expiry_sync_due_at IS NULL) OR "
        "(auto_cancelled_at IS NOT NULL AND status = 'cancelled')",
        name="ck_ftp_auto_cancellation",
    ),
    sa.CheckConstraint(
        "status IN ('publishing','pending','claimed','cancelled','failed')",
        name="ck_ftp_training_request_status",
    ),
    sa.CheckConstraint(
        "char_length(reason) <= 500",
        name="ck_ftp_training_request_reason_length",
    ),
    sa.CheckConstraint(
        "training_phase IS NULL OR training_phase IN ('phase_1','phase_2','phase_3')",
        name="ck_ftp_training_request_phase",
    ),
    sa.CheckConstraint(
        "requested_instructor_level IS NULL OR requested_instructor_level IN ('general','supervisor')",
        name="ck_ftp_training_request_instructor_level",
    ),
    sa.UniqueConstraint(
        "guild_id",
        "channel_id",
        "message_id",
        name="uq_ftp_training_request_message",
    ),
    sa.Index(
        "ix_ftp_training_request_guild_status",
        "guild_id",
        "status",
        "created_at",
    ),
    sa.Index(
        "ix_ftp_pending_expiry", "guild_id", sa.text("COALESCE(scheduled_for, created_at)"),
        postgresql_where=sa.text("status = 'pending'"),
    ),
    sa.Index(
        "ix_ftp_expiry_sync", "guild_id", "expiry_sync_due_at",
        postgresql_where=sa.text("expiry_sync_due_at IS NOT NULL"),
    ),
)

ftp_request_panels = sa.Table(
    "ftp_request_panels",
    metadata,
    sa.Column(
        "guild_id",
        sa.BigInteger,
        sa.ForeignKey("discord_guilds.discord_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("channel_id", sa.BigInteger, nullable=False),
    sa.Column("message_id", sa.BigInteger, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.UniqueConstraint(
        "guild_id",
        "channel_id",
        "message_id",
        name="uq_ftp_request_panel_message",
    ),
)

warrant_notifications = sa.Table(
    "warrant_notifications",
    metadata,
    uuid_column(primary_key=True),
    sa.Column(
        "guild_id",
        sa.BigInteger,
        sa.ForeignKey("discord_guilds.discord_id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("interaction_id", sa.BigInteger, nullable=False, unique=True),
    sa.Column("channel_id", sa.BigInteger, nullable=False),
    sa.Column("message_id", sa.BigInteger),
    sa.Column("requester_user_id", sa.BigInteger, nullable=False),
    sa.Column("report_url", sa.String(1000), nullable=False),
    sa.Column("warrant_id", sa.BigInteger, nullable=False),
    sa.Column("warrant_type", sa.String(20), nullable=False),
    sa.Column("status", sa.String(20), nullable=False),
    sa.Column("resolved_by_user_id", sa.BigInteger),
    sa.Column("rejection_reason", sa.String(500)),
    sa.Column("result_message_id", sa.BigInteger),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.Column("resolved_at", sa.DateTime(timezone=True)),
    sa.CheckConstraint(
        "status IN ('publishing','pending','signed','rejected','failed')",
        name="ck_warrant_notification_status",
    ),
    sa.CheckConstraint("warrant_id > 0", name="ck_warrant_notification_warrant_id"),
    sa.CheckConstraint(
        "rejection_reason IS NULL OR char_length(rejection_reason) <= 500",
        name="ck_warrant_notification_rejection_reason",
    ),
    sa.UniqueConstraint("guild_id", "channel_id", "message_id", name="uq_warrant_notification_message"),
    sa.Index("ix_warrant_notification_guild_status", "guild_id", "status", "created_at"),
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

ci_workflow_events = sa.Table(
    "ci_workflow_events",
    metadata,
    uuid_column(primary_key=True),
    sa.Column("provider_event_key", sa.String(64), nullable=False, unique=True),
    sa.Column("delivery_id", sa.String(64), nullable=False, unique=True),
    sa.Column("repository_id", sa.BigInteger, nullable=False),
    sa.Column("repository_full_name", sa.String(255), nullable=False),
    sa.Column("workflow_id", sa.BigInteger, nullable=False),
    sa.Column("workflow_name", sa.String(255), nullable=False),
    sa.Column("run_id", sa.BigInteger, nullable=False),
    sa.Column("run_attempt", sa.Integer, nullable=False),
    sa.Column("triggering_event", sa.String(50), nullable=False),
    sa.Column("branch", sa.String(255)),
    sa.Column("commit_hash", sa.String(64), nullable=False),
    sa.Column("commit_message", sa.String(500)),
    sa.Column("actor_login", sa.String(100), nullable=False),
    sa.Column("event_timestamp", sa.DateTime(timezone=True), nullable=False),
    sa.Column("status", sa.String(20), nullable=False, server_default="received"),
    sa.Column("ignored_reason", sa.String(100)),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.Column("processed_at", sa.DateTime(timezone=True)),
    sa.CheckConstraint("run_attempt > 0", name="ck_ci_workflow_run_attempt"),
    sa.CheckConstraint(
        "status IN ('received','processed','ignored')",
        name="ck_ci_workflow_event_status",
    ),
    sa.UniqueConstraint(
        "repository_id",
        "run_id",
        "run_attempt",
        name="uq_ci_workflow_run_attempt",
    ),
    sa.Index("ix_ci_workflow_event_status_created", "status", "created_at"),
)

ci_notification_deliveries = sa.Table(
    "ci_notification_deliveries",
    metadata,
    uuid_column(primary_key=True),
    sa.Column(
        "event_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("ci_workflow_events.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column(
        "guild_id",
        sa.BigInteger,
        sa.ForeignKey("discord_guilds.discord_id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("channel_id", sa.BigInteger, nullable=False),
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
        "status IN ('pending','retry','sent','failed')",
        name="ck_ci_notification_status",
    ),
    sa.Index("ix_ci_notification_pending", "status", "next_attempt_at"),
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

logbook_counters = sa.Table(
    "logbook_counters",
    metadata,
    sa.Column(
        "guild_id",
        sa.BigInteger,
        sa.ForeignKey("discord_guilds.discord_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("last_number", sa.BigInteger, nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.CheckConstraint("last_number > 0", name="ck_logbook_counter_positive"),
)

logbook_entries = sa.Table(
    "logbook_entries",
    metadata,
    uuid_column(primary_key=True),
    sa.Column(
        "guild_id",
        sa.BigInteger,
        sa.ForeignKey("discord_guilds.discord_id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("number", sa.BigInteger, nullable=False),
    sa.Column("channel_id", sa.BigInteger, nullable=False),
    sa.Column("message_id", sa.BigInteger),
    sa.Column("interaction_id", sa.BigInteger, nullable=False, unique=True),
    sa.Column("author_user_id", sa.BigInteger, nullable=False),
    sa.Column("category_key", sa.String(30), nullable=False),
    sa.Column("subtype_key", sa.String(40), nullable=False),
    sa.Column("subject", sa.String(200), nullable=False),
    sa.Column("description", sa.Text, nullable=False),
    sa.Column("money_direction", sa.String(20)),
    sa.Column("money_amount", sa.Numeric(16, 2)),
    sa.Column("money_detail", sa.String(300)),
    sa.Column("status", sa.String(20), nullable=False, server_default="publishing"),
    sa.Column("version", sa.Integer, nullable=False, server_default="1"),
    sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.CheckConstraint(
        "category_key IN ('finance','personnel','assignments','equipment','orders',"
        "'evidence','fleet','other')",
        name="ck_logbook_category",
    ),
    sa.CheckConstraint(
        "money_direction IS NULL OR money_direction IN ('income','withdrawal')",
        name="ck_logbook_money_direction",
    ),
    sa.CheckConstraint(
        "(money_direction IS NULL AND money_amount IS NULL) OR "
        "(money_direction IS NOT NULL AND money_amount > 0)",
        name="ck_logbook_money_pair",
    ),
    sa.CheckConstraint(
        "status IN ('publishing','published','failed','voided')",
        name="ck_logbook_status",
    ),
    sa.CheckConstraint("number > 0", name="ck_logbook_number_positive"),
    sa.CheckConstraint("version > 0", name="ck_logbook_version_positive"),
    sa.UniqueConstraint("guild_id", "number", name="uq_logbook_guild_number"),
    sa.UniqueConstraint("guild_id", "channel_id", "message_id", name="uq_logbook_message"),
    sa.Index("ix_logbook_guild_created", "guild_id", "created_at"),
    sa.Index("ix_logbook_author_created", "guild_id", "author_user_id", "created_at"),
)

logbook_assets = sa.Table(
    "logbook_assets",
    metadata,
    uuid_column(primary_key=True),
    sa.Column(
        "entry_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("logbook_entries.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("position", sa.Integer, nullable=False),
    sa.Column("object_key", sa.String(500), nullable=False),
    sa.Column("sha256", sa.String(64), nullable=False),
    sa.Column("filename", sa.String(255), nullable=False),
    sa.Column("content_type", sa.String(100), nullable=False),
    sa.Column("size_bytes", sa.BigInteger, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.CheckConstraint("position >= 0", name="ck_logbook_asset_position"),
    sa.CheckConstraint("size_bytes > 0", name="ck_logbook_asset_size"),
    sa.UniqueConstraint("entry_id", "position", name="uq_logbook_asset_position"),
)

report_issue_types = sa.Table(
    "report_issue_types",
    metadata,
    sa.Column("code", sa.String(50), primary_key=True),
    sa.Column("display_name", sa.String(100), nullable=False),
    sa.Column("description", sa.String(500), nullable=False),
    sa.Column("position", sa.Integer, nullable=False),
    sa.Column("active", sa.Boolean, nullable=False, server_default=sa.true()),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.UniqueConstraint("position", name="uq_report_issue_type_position"),
)

report_reviews = sa.Table(
    "report_reviews",
    metadata,
    uuid_column(primary_key=True),
    sa.Column(
        "guild_id",
        sa.BigInteger,
        sa.ForeignKey("discord_guilds.discord_id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("parent_channel_id", sa.BigInteger, nullable=False),
    sa.Column("thread_id", sa.BigInteger),
    sa.Column("root_message_id", sa.BigInteger, nullable=False),
    sa.Column("trigger_message_id", sa.BigInteger, nullable=False),
    sa.Column("report_author_user_id", sa.BigInteger),
    sa.Column("requester_user_id", sa.BigInteger, nullable=False),
    sa.Column("attempt_number", sa.Integer, nullable=False),
    sa.Column("review_scope", sa.String(20), nullable=False),
    sa.Column("verdict", sa.String(30), nullable=False),
    sa.Column("status", sa.String(20), nullable=False),
    sa.Column("model", sa.String(100), nullable=False),
    sa.Column("reasoning_effort", sa.String(30), nullable=False),
    sa.Column("prompt_version", sa.String(40), nullable=False),
    sa.Column("persona_id", sa.String(80), nullable=False),
    sa.Column("persona_revision", sa.Integer, nullable=False),
    sa.Column("app_version", sa.String(30), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.Column("published_at", sa.DateTime(timezone=True)),
    sa.Column("failed_at", sa.DateTime(timezone=True)),
    sa.CheckConstraint("attempt_number > 0", name="ck_report_review_attempt_positive"),
    sa.CheckConstraint(
        "review_scope IN ('full','narrative','charges','targeted')",
        name="ck_report_review_scope",
    ),
    sa.CheckConstraint(
        "verdict IN ('approved','corrections_required','inconclusive')",
        name="ck_report_review_verdict",
    ),
    sa.CheckConstraint(
        "status IN ('publishing','published','failed')",
        name="ck_report_review_status",
    ),
    sa.UniqueConstraint("guild_id", "trigger_message_id", name="uq_report_review_trigger"),
    sa.UniqueConstraint(
        "guild_id",
        "root_message_id",
        "attempt_number",
        name="uq_report_review_attempt",
    ),
    sa.Index(
        "ix_report_review_guild_published_created",
        "guild_id",
        "status",
        "created_at",
    ),
    sa.Index(
        "ix_report_review_guild_root",
        "guild_id",
        "root_message_id",
        "attempt_number",
    ),
)

report_review_findings = sa.Table(
    "report_review_findings",
    metadata,
    uuid_column(primary_key=True),
    sa.Column(
        "review_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("report_reviews.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column(
        "issue_code",
        sa.String(50),
        sa.ForeignKey("report_issue_types.code", ondelete="RESTRICT"),
        nullable=False,
    ),
    sa.Column("severity", sa.String(20), nullable=False),
    sa.Column("position", sa.Integer, nullable=False),
    sa.CheckConstraint(
        "severity IN ('required','warning','observation')",
        name="ck_report_review_finding_severity",
    ),
    sa.CheckConstraint("position >= 0", name="ck_report_review_finding_position"),
    sa.UniqueConstraint("review_id", "position", name="uq_report_review_finding_position"),
    sa.Index("ix_report_review_finding_issue", "issue_code", "severity"),
)

report_owner_agent_runs = sa.Table(
    "report_owner_agent_runs",
    metadata,
    uuid_column(primary_key=True),
    sa.Column(
        "guild_id",
        sa.BigInteger,
        sa.ForeignKey("discord_guilds.discord_id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("root_message_id", sa.BigInteger, nullable=False),
    sa.Column("trigger_message_id", sa.BigInteger, nullable=False),
    sa.Column("owner_user_id", sa.BigInteger, nullable=False),
    sa.Column("thread_id", sa.BigInteger),
    sa.Column("status", sa.String(20), nullable=False),
    sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.Column("completed_at", sa.DateTime(timezone=True)),
    sa.Column("failed_at", sa.DateTime(timezone=True)),
    sa.CheckConstraint(
        "status IN ('started','completed','failed')",
        name="ck_report_owner_agent_run_status",
    ),
    sa.UniqueConstraint("trigger_message_id", name="uq_report_owner_agent_run_trigger"),
    sa.Index(
        "ix_report_owner_agent_run_usage",
        "guild_id",
        "root_message_id",
        "owner_user_id",
        "started_at",
    ),
)

report_owner_round_grants = sa.Table(
    "report_owner_round_grants",
    metadata,
    uuid_column(primary_key=True),
    sa.Column(
        "guild_id",
        sa.BigInteger,
        sa.ForeignKey("discord_guilds.discord_id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("root_message_id", sa.BigInteger, nullable=False),
    sa.Column("owner_user_id", sa.BigInteger, nullable=False),
    sa.Column("granted_by_user_id", sa.BigInteger, nullable=False),
    sa.Column("interaction_id", sa.BigInteger, nullable=False, unique=True),
    sa.Column("amount", sa.Integer, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.CheckConstraint("amount BETWEEN 1 AND 10", name="ck_report_owner_round_grant_amount"),
    sa.Index(
        "ix_report_owner_round_grant_usage",
        "guild_id",
        "root_message_id",
        "owner_user_id",
    ),
)

report_owner_quota_exhaustion_notifications = sa.Table(
    "report_owner_quota_exhaustion_notifications",
    metadata,
    uuid_column(primary_key=True),
    sa.Column(
        "guild_id",
        sa.BigInteger,
        sa.ForeignKey("discord_guilds.discord_id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("root_message_id", sa.BigInteger, nullable=False),
    sa.Column("owner_user_id", sa.BigInteger, nullable=False),
    sa.Column("max_rounds", sa.Integer, nullable=False),
    sa.Column("message_id", sa.BigInteger),
    sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.Column("sent_at", sa.DateTime(timezone=True)),
    sa.CheckConstraint("max_rounds > 0", name="ck_report_owner_quota_notification_max"),
    sa.UniqueConstraint(
        "guild_id",
        "root_message_id",
        "owner_user_id",
        "max_rounds",
        name="uq_report_owner_quota_notification_threshold",
    ),
)

manual_quiz_questions = sa.Table(
    "manual_quiz_questions",
    metadata,
    uuid_column(primary_key=True),
    sa.Column(
        "guild_id",
        sa.BigInteger,
        sa.ForeignKey("discord_guilds.discord_id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("slug", sa.String(120), nullable=False),
    sa.Column("answer_mode", sa.String(30), nullable=False),
    sa.Column("category", sa.String(30), nullable=False),
    sa.Column("difficulty", sa.String(20), nullable=False),
    sa.Column("prompt", sa.Text, nullable=False),
    sa.Column("explanation", sa.Text, nullable=False),
    sa.Column("source_reference", sa.String(300), nullable=False),
    sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
    sa.Column("revision", sa.Integer, nullable=False, server_default="1"),
    sa.Column("created_by", sa.BigInteger),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.CheckConstraint(
        "slug ~ '^[a-z0-9]+(_[a-z0-9]+)*$'",
        name="ck_manual_quiz_slug_format",
    ),
    sa.CheckConstraint(
        "answer_mode IN ('single_choice','true_false','multiple_select','short_answer',"
        "'ordering','matching')",
        name="ck_manual_quiz_answer_mode",
    ),
    sa.CheckConstraint(
        "category IN ('general','radio_code','procedure','scenario','identify_error','legal','reports')",
        name="ck_manual_quiz_category",
    ),
    sa.CheckConstraint(
        "difficulty IN ('basic','intermediate','advanced')",
        name="ck_manual_quiz_difficulty",
    ),
    sa.CheckConstraint(
        "status IN ('draft','active','archived')",
        name="ck_manual_quiz_status",
    ),
    sa.CheckConstraint("revision > 0", name="ck_manual_quiz_revision_positive"),
    sa.CheckConstraint(
        "char_length(prompt) BETWEEN 1 AND 1500",
        name="ck_manual_quiz_prompt_length",
    ),
    sa.CheckConstraint(
        "char_length(explanation) BETWEEN 1 AND 1500",
        name="ck_manual_quiz_explanation_length",
    ),
    sa.CheckConstraint(
        "char_length(source_reference) BETWEEN 1 AND 300",
        name="ck_manual_quiz_source_length",
    ),
    sa.Index(
        "ix_manual_quiz_guild_status_mode",
        "guild_id",
        "status",
        "answer_mode",
    ),
    sa.UniqueConstraint("guild_id", "slug", name="uq_manual_quiz_guild_slug"),
)

manual_quiz_options = sa.Table(
    "manual_quiz_options",
    metadata,
    uuid_column(primary_key=True),
    sa.Column(
        "question_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("manual_quiz_questions.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("position", sa.Integer, nullable=False),
    sa.Column("text", sa.String(300), nullable=False),
    sa.Column("is_correct", sa.Boolean, nullable=False, server_default=sa.false()),
    sa.CheckConstraint(
        "position BETWEEN 0 AND 24",
        name="ck_manual_quiz_option_position",
    ),
    sa.CheckConstraint(
        "char_length(text) BETWEEN 1 AND 300",
        name="ck_manual_quiz_option_text_length",
    ),
    sa.UniqueConstraint(
        "question_id",
        "position",
        name="uq_manual_quiz_option_position",
    ),
    sa.Index("ix_manual_quiz_option_question", "question_id", "position"),
)

manual_quiz_rotations = sa.Table(
    "manual_quiz_rotations",
    metadata,
    sa.Column(
        "guild_id",
        sa.BigInteger,
        sa.ForeignKey("discord_guilds.discord_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("answer_mode_filter", sa.String(30), primary_key=True),
    sa.Column("difficulty_filter", sa.String(20), primary_key=True),
    sa.Column("cycle", sa.Integer, nullable=False, server_default="1"),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.CheckConstraint(
        "answer_mode_filter IN ('any','single_choice','true_false')",
        name="ck_manual_quiz_rotation_answer_mode",
    ),
    sa.CheckConstraint(
        "difficulty_filter IN ('any','basic','intermediate','advanced')",
        name="ck_manual_quiz_rotation_difficulty",
    ),
    sa.CheckConstraint("cycle > 0", name="ck_manual_quiz_rotation_cycle_positive"),
)

manual_quiz_rotation_entries = sa.Table(
    "manual_quiz_rotation_entries",
    metadata,
    sa.Column("guild_id", sa.BigInteger, primary_key=True),
    sa.Column("answer_mode_filter", sa.String(30), primary_key=True),
    sa.Column("difficulty_filter", sa.String(20), primary_key=True),
    sa.Column("cycle", sa.Integer, primary_key=True),
    sa.Column(
        "question_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("manual_quiz_questions.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("presented_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.ForeignKeyConstraint(
        ["guild_id", "answer_mode_filter", "difficulty_filter"],
        [
            "manual_quiz_rotations.guild_id",
            "manual_quiz_rotations.answer_mode_filter",
            "manual_quiz_rotations.difficulty_filter",
        ],
        ondelete="CASCADE",
        name="fk_manual_quiz_rotation_entry_scope",
    ),
    sa.CheckConstraint("cycle > 0", name="ck_manual_quiz_rotation_entry_cycle_positive"),
    sa.Index(
        "ix_manual_quiz_rotation_entry_scope",
        "guild_id",
        "answer_mode_filter",
        "difficulty_filter",
        "cycle",
    ),
)

service_call_cursors = sa.Table(
    "service_call_cursors",
    metadata,
    sa.Column(
        "guild_id",
        sa.BigInteger,
        sa.ForeignKey("discord_guilds.discord_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("channel_id", sa.BigInteger, primary_key=True),
    sa.Column("last_call_id", sa.BigInteger, nullable=False),
    sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    ),
    sa.Column(
        "updated_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    ),
    sa.CheckConstraint("last_call_id > 0", name="ck_service_call_cursor_positive"),
)

stolen_vehicle_feed_cursors = sa.Table(
    "stolen_vehicle_feed_cursors",
    metadata,
    sa.Column(
        "guild_id",
        sa.BigInteger,
        sa.ForeignKey("discord_guilds.discord_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("channel_id", sa.BigInteger, primary_key=True),
    sa.Column("last_report_id", sa.BigInteger, nullable=False),
    sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    ),
    sa.Column(
        "updated_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    ),
    sa.CheckConstraint(
        "last_report_id >= 0",
        name="ck_stolen_vehicle_feed_cursor_nonnegative",
    ),
)

report_arrest_bindings = sa.Table(
    "report_arrest_bindings",
    metadata,
    sa.Column(
        "guild_id",
        sa.BigInteger,
        sa.ForeignKey("discord_guilds.discord_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("root_message_id", sa.BigInteger, primary_key=True),
    sa.Column("parent_channel_id", sa.BigInteger, nullable=False),
    sa.Column("thread_id", sa.BigInteger, nullable=False),
    sa.Column("sarp_arrest_id", sa.BigInteger, nullable=False),
    sa.Column("bound_by_user_id", sa.BigInteger, nullable=False),
    sa.Column("trigger_message_id", sa.BigInteger, nullable=False),
    sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    ),
    sa.Column(
        "updated_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    ),
    sa.CheckConstraint("sarp_arrest_id > 0", name="ck_report_arrest_binding_positive"),
    sa.Index("ix_report_arrest_binding_arrest", "guild_id", "sarp_arrest_id"),
)

arrest_report_submissions = sa.Table(
    "arrest_report_submissions",
    metadata,
    uuid_column(primary_key=True),
    sa.Column(
        "guild_id",
        sa.BigInteger,
        sa.ForeignKey("discord_guilds.discord_id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("interaction_id", sa.BigInteger, nullable=False, unique=True),
    sa.Column("owner_user_id", sa.BigInteger, nullable=False),
    sa.Column("parent_channel_id", sa.BigInteger, nullable=False),
    sa.Column("root_message_id", sa.BigInteger),
    sa.Column("thread_id", sa.BigInteger),
    sa.Column("sarp_arrest_id", sa.BigInteger),
    sa.Column("suspect_name", sa.String(100), nullable=False),
    sa.Column("publication_status", sa.String(20), nullable=False),
    sa.Column("initial_review_status", sa.String(20), nullable=False),
    sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    ),
    sa.Column(
        "updated_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    ),
    sa.CheckConstraint(
        "sarp_arrest_id IS NULL OR sarp_arrest_id > 0",
        name="ck_arrest_submission_report_positive",
    ),
    sa.CheckConstraint(
        "publication_status IN ('publishing','published','failed')",
        name="ck_arrest_submission_publication_status",
    ),
    sa.CheckConstraint(
        "initial_review_status IN ('pending','running','completed','failed')",
        name="ck_arrest_submission_review_status",
    ),
    sa.Index("ix_arrest_submission_guild_report", "guild_id", "sarp_arrest_id"),
    sa.UniqueConstraint(
        "guild_id",
        "root_message_id",
        name="uq_arrest_submission_guild_root",
    ),
    sa.Index("ix_arrest_submission_thread", "guild_id", "thread_id"),
)

arrest_report_assets = sa.Table(
    "arrest_report_assets",
    metadata,
    uuid_column(primary_key=True),
    sa.Column(
        "submission_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("arrest_report_submissions.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("category", sa.String(30), nullable=False),
    sa.Column("position", sa.Integer, nullable=False),
    sa.Column("object_key", sa.String(500), nullable=False),
    sa.Column("sha256", sa.String(64), nullable=False),
    sa.Column("filename", sa.String(255), nullable=False),
    sa.Column("content_type", sa.String(100), nullable=False),
    sa.Column("size_bytes", sa.BigInteger, nullable=False),
    sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    ),
    sa.CheckConstraint(
        "category IN ('suspect_photo','supporting_image')",
        name="ck_arrest_report_asset_category",
    ),
    sa.CheckConstraint("position >= 0", name="ck_arrest_report_asset_position"),
    sa.CheckConstraint("size_bytes > 0", name="ck_arrest_report_asset_size"),
    sa.UniqueConstraint(
        "submission_id",
        "category",
        "position",
        name="uq_arrest_report_asset_position",
    ),
)

role_requests = sa.Table(
    "role_requests",
    metadata,
    uuid_column(primary_key=True),
    sa.Column(
        "guild_id",
        sa.BigInteger,
        sa.ForeignKey("discord_guilds.discord_id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("interaction_id", sa.BigInteger, nullable=False, unique=True),
    sa.Column("requester_id", sa.BigInteger, nullable=False),
    sa.Column("kind", sa.String(20), nullable=False),
    sa.Column("add_role_ids", postgresql.ARRAY(sa.BigInteger), nullable=False),
    sa.Column("remove_role_ids", postgresql.ARRAY(sa.BigInteger), nullable=False),
    sa.Column("reason", sa.Text),
    sa.Column("status", sa.String(30), nullable=False),
    sa.Column("public_channel_id", sa.BigInteger, nullable=False),
    sa.Column("public_message_id", sa.BigInteger),
    sa.Column("reviewer_id", sa.BigInteger),
    sa.Column("decision_comment", sa.Text),
    sa.Column("approved_add_role_ids", postgresql.ARRAY(sa.BigInteger), nullable=False, server_default="{}"),
    sa.Column(
        "approved_remove_role_ids",
        postgresql.ARRAY(sa.BigInteger),
        nullable=False,
        server_default="{}",
    ),
    sa.Column(
        "unapproved_add_role_ids",
        postgresql.ARRAY(sa.BigInteger),
        nullable=False,
        server_default="{}",
    ),
    sa.Column(
        "unapproved_remove_role_ids",
        postgresql.ARRAY(sa.BigInteger),
        nullable=False,
        server_default="{}",
    ),
    sa.Column("requester_dm_delivered", sa.Boolean),
    sa.Column("version", sa.Integer, nullable=False, server_default="1"),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.Column("decided_at", sa.DateTime(timezone=True)),
    sa.CheckConstraint(
        "kind IN ('normal','retirement','ftt_entry')",
        name="ck_role_request_kind",
    ),
    sa.CheckConstraint(
        "status IN ('publishing','pending','processing','approved','partially_approved','rejected','failed')",
        name="ck_role_request_status",
    ),
    sa.CheckConstraint("version > 0", name="ck_role_request_version_positive"),
    sa.UniqueConstraint(
        "guild_id", "public_channel_id", "public_message_id", name="uq_role_request_public_message"
    ),
    sa.Index("ix_role_request_requester", "guild_id", "requester_id", "created_at"),
    sa.Index("ix_role_request_status", "guild_id", "status", "created_at"),
    sa.Index(
        "uq_role_request_live_requester",
        "guild_id",
        "requester_id",
        unique=True,
        postgresql_where=sa.text("status IN ('publishing','pending','processing')"),
    ),
)

role_request_review_messages = sa.Table(
    "role_request_review_messages",
    metadata,
    uuid_column(primary_key=True),
    sa.Column(
        "request_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("role_requests.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("channel_id", sa.BigInteger, nullable=False),
    sa.Column("message_id", sa.BigInteger, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.UniqueConstraint("request_id", "channel_id", name="uq_role_request_review_channel"),
    sa.UniqueConstraint("channel_id", "message_id", name="uq_role_request_review_message"),
    sa.Index("ix_role_request_review_request", "request_id"),
)

agent_polls = sa.Table(
    "agent_polls",
    metadata,
    sa.Column("message_id", sa.BigInteger, primary_key=True),
    sa.Column(
        "guild_id",
        sa.BigInteger,
        sa.ForeignKey("discord_guilds.discord_id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("channel_id", sa.BigInteger, nullable=False),
    sa.Column("creator_user_id", sa.BigInteger, nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("ended_at", sa.DateTime(timezone=True)),
    sa.Column("deleted_at", sa.DateTime(timezone=True)),
    sa.Column("last_synced_at", sa.DateTime(timezone=True)),
    sa.Column("last_activity_at", sa.DateTime(timezone=True)),
    sa.CheckConstraint("expires_at > created_at", name="ck_agent_poll_expiry_after_creation"),
    sa.Index("ix_agent_poll_guild_created", "guild_id", "created_at"),
    sa.Index("ix_agent_poll_creator_created", "guild_id", "creator_user_id", "created_at"),
    sa.Index("ix_agent_poll_channel_created", "guild_id", "channel_id", "created_at"),
)

supporter_profiles = sa.Table(
    "supporter_profiles",
    metadata,
    uuid_column(primary_key=True),
    sa.Column("discord_user_id", sa.BigInteger, nullable=False, unique=True),
    sa.Column("display_name", sa.String(200)),
    sa.Column("mention_enabled", sa.Boolean, nullable=False, server_default=sa.true()),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.CheckConstraint("discord_user_id > 0", name="ck_supporter_profile_discord_user"),
)

supporter_provider_links = sa.Table(
    "supporter_provider_links",
    metadata,
    uuid_column(primary_key=True),
    sa.Column(
        "profile_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("supporter_profiles.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("provider", sa.String(50), nullable=False),
    sa.Column("provider_supporter_id", sa.String(100), nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.UniqueConstraint("provider", "provider_supporter_id", name="uq_supporter_provider_identity"),
    sa.Index("ix_supporter_provider_link_profile", "profile_id"),
)

support_exchange_rates = sa.Table(
    "support_exchange_rates",
    metadata,
    uuid_column(primary_key=True),
    sa.Column("source", sa.String(50), nullable=False),
    sa.Column("requested_date", sa.Date, nullable=False),
    sa.Column("effective_date", sa.Date, nullable=False),
    sa.Column("base_currency", sa.String(3), nullable=False),
    sa.Column("quote_currency", sa.String(3), nullable=False),
    sa.Column("rate", sa.Numeric(24, 12), nullable=False),
    sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.CheckConstraint("rate > 0", name="ck_support_exchange_rate_positive"),
    sa.CheckConstraint(
        "base_currency <> quote_currency",
        name="ck_support_exchange_rate_distinct_currencies",
    ),
    sa.UniqueConstraint(
        "source",
        "requested_date",
        "base_currency",
        "quote_currency",
        name="uq_support_exchange_rate_lookup",
    ),
)

support_contributions = sa.Table(
    "support_contributions",
    metadata,
    uuid_column(primary_key=True),
    sa.Column("provider", sa.String(50), nullable=False),
    sa.Column("provider_contribution_id", sa.String(100), nullable=False),
    sa.Column("transaction_id", sa.String(255)),
    sa.Column("provider_supporter_id", sa.String(100)),
    sa.Column(
        "supporter_profile_id",
        postgresql.UUID(as_uuid=True),
        sa.ForeignKey("supporter_profiles.id", ondelete="SET NULL"),
    ),
    sa.Column("supporter_name", sa.String(200)),
    sa.Column("supporter_name_type", sa.String(50)),
    sa.Column("original_amount", sa.Numeric(18, 6), nullable=False),
    sa.Column("original_currency", sa.String(3), nullable=False),
    sa.Column("normalized_amount_usd", sa.Numeric(18, 6)),
    sa.Column("exchange_rate_to_usd", sa.Numeric(24, 12)),
    sa.Column("exchange_rate_source", sa.String(50)),
    sa.Column("exchange_rate_date", sa.Date),
    sa.Column("normalization_status", sa.String(20), nullable=False, server_default="pending"),
    sa.Column("normalization_attempts", sa.Integer, nullable=False, server_default="0"),
    sa.Column(
        "normalization_next_attempt_at",
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.func.now(),
    ),
    sa.Column("normalization_last_error", sa.String(500)),
    sa.Column("normalized_at", sa.DateTime(timezone=True)),
    sa.Column("coffee_count", sa.Integer),
    sa.Column("coffee_price", sa.Numeric(18, 6)),
    sa.Column("total_amount_charged", sa.Numeric(18, 6)),
    sa.Column("application_fee", sa.Numeric(18, 6)),
    sa.Column("status", sa.String(20), nullable=False),
    sa.Column("contributed_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("refunded_at", sa.DateTime(timezone=True)),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.CheckConstraint("original_amount >= 0", name="ck_support_contribution_original_amount"),
    sa.CheckConstraint(
        "normalized_amount_usd IS NULL OR normalized_amount_usd >= 0",
        name="ck_support_contribution_normalized_amount",
    ),
    sa.CheckConstraint(
        "exchange_rate_to_usd IS NULL OR exchange_rate_to_usd > 0",
        name="ck_support_contribution_exchange_rate",
    ),
    sa.CheckConstraint(
        "coffee_count IS NULL OR coffee_count >= 0",
        name="ck_support_contribution_coffee_count",
    ),
    sa.CheckConstraint(
        "status IN ('succeeded','refunded')",
        name="ck_support_contribution_status",
    ),
    sa.CheckConstraint(
        "normalization_status IN ('pending','completed','retry','failed')",
        name="ck_support_contribution_normalization_status",
    ),
    sa.CheckConstraint(
        "normalization_attempts >= 0",
        name="ck_support_contribution_normalization_attempts",
    ),
    sa.CheckConstraint(
        "normalization_status <> 'completed' OR "
        "(normalized_amount_usd IS NOT NULL AND exchange_rate_to_usd IS NOT NULL "
        "AND exchange_rate_source IS NOT NULL AND exchange_rate_date IS NOT NULL "
        "AND normalized_at IS NOT NULL)",
        name="ck_support_contribution_completed_normalization",
    ),
    sa.UniqueConstraint(
        "provider",
        "provider_contribution_id",
        name="uq_support_contribution_provider_id",
    ),
    sa.Index("ix_support_contribution_status_date", "status", "contributed_at"),
    sa.Index(
        "ix_support_contribution_supporter",
        "provider",
        "provider_supporter_id",
    ),
    sa.Index("ix_support_contribution_profile", "supporter_profile_id"),
    sa.Index(
        "ix_support_contribution_normalization_due",
        "normalization_status",
        "normalization_next_attempt_at",
    ),
)

support_webhook_events = sa.Table(
    "support_webhook_events",
    metadata,
    uuid_column(primary_key=True),
    sa.Column("provider_event_key", sa.String(64), nullable=False, unique=True),
    sa.Column("provider", sa.String(50), nullable=False),
    sa.Column("provider_event_id", sa.String(100), nullable=False),
    sa.Column("event_type", sa.String(30), nullable=False),
    sa.Column("live_mode", sa.Boolean, nullable=False),
    sa.Column("attempt", sa.Integer, nullable=False),
    sa.Column("provider_contribution_id", sa.String(100), nullable=False),
    sa.Column("payload_sha256", sa.String(64), nullable=False),
    sa.Column("event_timestamp", sa.DateTime(timezone=True), nullable=False),
    sa.Column("status", sa.String(20), nullable=False),
    sa.Column("ignored_reason", sa.String(100)),
    sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.CheckConstraint("event_type IN ('created','refunded')", name="ck_support_event_type"),
    sa.CheckConstraint("attempt > 0", name="ck_support_event_attempt"),
    sa.CheckConstraint(
        "status IN ('processed','ignored')",
        name="ck_support_event_status",
    ),
    sa.Index("ix_support_webhook_event_received", "provider", "received_at"),
)

blacklist_counters = sa.Table(
    "blacklist_counters",
    metadata,
    sa.Column(
        "guild_id",
        sa.BigInteger,
        sa.ForeignKey("discord_guilds.discord_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("type_key", sa.String(40), primary_key=True),
    sa.Column("last_number", sa.BigInteger, nullable=False),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.CheckConstraint("last_number > 0", name="ck_blacklist_counter_positive"),
)

blacklist_entries = sa.Table(
    "blacklist_entries",
    metadata,
    uuid_column(primary_key=True),
    sa.Column(
        "guild_id",
        sa.BigInteger,
        sa.ForeignKey("discord_guilds.discord_id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("type_key", sa.String(40), nullable=False),
    sa.Column("type_name", sa.String(50), nullable=False),
    sa.Column("reference_prefix", sa.String(12), nullable=False),
    sa.Column("number", sa.BigInteger, nullable=False),
    sa.Column("channel_id", sa.BigInteger, nullable=False),
    sa.Column("message_id", sa.BigInteger),
    sa.Column("interaction_id", sa.BigInteger, nullable=False, unique=True),
    sa.Column("author_user_id", sa.BigInteger, nullable=False),
    sa.Column("profile_kind", sa.String(20), nullable=False),
    sa.Column("pcu_profile_ids", postgresql.ARRAY(sa.BigInteger), nullable=False),
    sa.Column("pcu_urls", postgresql.ARRAY(sa.String(200)), nullable=False),
    sa.Column("subject_label", sa.String(30), nullable=False),
    sa.Column("subject_name", sa.String(100), nullable=False),
    sa.Column("subject_name_key", sa.String(100), nullable=False),
    sa.Column("reason", sa.Text, nullable=False),
    sa.Column("status", sa.String(20), nullable=False, server_default="publishing"),
    sa.Column("edited_by_user_id", sa.BigInteger),
    sa.Column("edited_at", sa.DateTime(timezone=True)),
    sa.Column("revoked_by_user_id", sa.BigInteger),
    sa.Column("revoked_at", sa.DateTime(timezone=True)),
    sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    sa.CheckConstraint("number > 0", name="ck_blacklist_number_positive"),
    sa.CheckConstraint(
        "profile_kind IN ('character','account')",
        name="ck_blacklist_profile_kind",
    ),
    sa.CheckConstraint(
        "cardinality(pcu_profile_ids) BETWEEN 1 AND 5",
        name="ck_blacklist_profile_count",
    ),
    sa.CheckConstraint(
        "0 < ALL(pcu_profile_ids)",
        name="ck_blacklist_profile_ids_positive",
    ),
    sa.CheckConstraint(
        "cardinality(pcu_urls) = cardinality(pcu_profile_ids)",
        name="ck_blacklist_profile_cardinality",
    ),
    sa.CheckConstraint(
        "status IN ('publishing','active','failed','revoked')",
        name="ck_blacklist_status",
    ),
    sa.UniqueConstraint(
        "guild_id",
        "type_key",
        "number",
        name="uq_blacklist_type_number",
    ),
    sa.UniqueConstraint(
        "guild_id",
        "channel_id",
        "message_id",
        name="uq_blacklist_message",
    ),
    sa.Index("ix_blacklist_profile_ids", "pcu_profile_ids", postgresql_using="gin"),
    sa.Index(
        "ix_blacklist_guild_subject",
        "guild_id",
        "subject_name_key",
        "status",
    ),
    sa.Index("ix_blacklist_guild_created", "guild_id", "created_at"),
)

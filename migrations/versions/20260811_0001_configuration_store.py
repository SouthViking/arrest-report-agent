"""Create versioned guild configuration and persona storage."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260811_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "discord_guilds",
        sa.Column("discord_id", sa.BigInteger(), primary_key=True),
        sa.Column("name", sa.String(100), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("active_config_revision_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "personas",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("slug", sa.String(80), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "persona_revisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "persona_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("personas.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('draft','published','archived')", name="ck_persona_revision_status"),
        sa.UniqueConstraint("persona_id", "revision", name="uq_persona_revision"),
        sa.UniqueConstraint("persona_id", "checksum", name="uq_persona_checksum"),
    )
    op.create_table(
        "persona_assets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
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
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("persona_revision_id", "kind", name="uq_persona_asset_kind"),
        sa.UniqueConstraint("object_key", name="uq_persona_asset_key"),
    )
    op.create_table(
        "guild_config_revisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "guild_id",
            sa.BigInteger(),
            sa.ForeignKey("discord_guilds.discord_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('draft','published','archived')", name="ck_guild_config_status"),
        sa.UniqueConstraint("guild_id", "revision", name="uq_guild_config_revision"),
    )
    op.create_table(
        "guild_agent_settings",
        sa.Column(
            "config_revision_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("guild_config_revisions.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("reasoning_effort", sa.String(30), nullable=False),
        sa.Column("review_timeout_seconds", sa.Float(), nullable=False),
        sa.Column("max_agent_turns", sa.Integer(), nullable=False),
        sa.Column("max_images_per_review", sa.Integer(), nullable=False),
        sa.Column("max_image_bytes", sa.BigInteger(), nullable=False),
        sa.Column("max_files_per_review", sa.Integer(), nullable=False),
        sa.Column("max_file_bytes", sa.BigInteger(), nullable=False),
        sa.Column("max_total_file_bytes", sa.BigInteger(), nullable=False),
        sa.Column("thread_context_max_messages", sa.Integer(), nullable=False),
        sa.Column("channel_context_max_messages", sa.Integer(), nullable=False),
        sa.Column("thread_context_max_chars", sa.Integer(), nullable=False),
        sa.Column("thread_context_max_message_chars", sa.Integer(), nullable=False),
        sa.Column("thread_context_max_historical_images", sa.Integer(), nullable=False),
        sa.Column("thread_context_max_historical_files", sa.Integer(), nullable=False),
        sa.Column("thread_auto_archive_minutes", sa.Integer(), nullable=False),
    )
    op.create_table(
        "guild_access_policies",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
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
    op.create_table(
        "guild_access_policy_members",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "policy_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("guild_access_policies.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("snowflake_id", sa.BigInteger(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint("policy_id", "snowflake_id", name="uq_policy_member"),
    )
    op.create_table(
        "guild_feature_settings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "config_revision_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("guild_config_revisions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("feature_key", sa.String(80), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.UniqueConstraint("config_revision_id", "feature_key", name="uq_config_feature"),
    )
    op.create_table(
        "guild_persona_assignments",
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
    op.create_table(
        "bot_application_profile",
        sa.Column("singleton_id", sa.SmallInteger(), primary_key=True),
        sa.Column(
            "persona_revision_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("persona_revisions.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("sync_status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.String(500), nullable=True),
        sa.Column("updated_by", sa.BigInteger(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("singleton_id = 1", name="ck_singleton_profile"),
        sa.CheckConstraint("sync_status IN ('pending','synced','failed')", name="ck_profile_sync_status"),
    )
    op.create_table(
        "config_audit_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "guild_id",
            sa.BigInteger(),
            sa.ForeignKey("discord_guilds.discord_id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("actor_user_id", sa.BigInteger(), nullable=True),
        sa.Column("discord_interaction_id", sa.BigInteger(), nullable=True),
        sa.Column("operation", sa.String(100), nullable=False),
        sa.Column(
            "config_revision_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("guild_config_revisions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("discord_interaction_id", name="uq_audit_interaction"),
    )
    op.create_table(
        "data_migrations",
        sa.Column("name", sa.String(120), primary_key=True),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_foreign_key(
        "fk_guild_active_revision",
        "discord_guilds",
        "guild_config_revisions",
        ["active_config_revision_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_config_revision_guild_status", "guild_config_revisions", ["guild_id", "status"])
    op.create_index("ix_persona_revision_status", "persona_revisions", ["persona_id", "status"])


def downgrade() -> None:
    op.drop_constraint("fk_guild_active_revision", "discord_guilds", type_="foreignkey")
    for table in (
        "data_migrations",
        "config_audit_events",
        "bot_application_profile",
        "guild_persona_assignments",
        "guild_feature_settings",
        "guild_access_policy_members",
        "guild_access_policies",
        "guild_agent_settings",
        "guild_config_revisions",
        "persona_assets",
        "persona_revisions",
        "personas",
        "discord_guilds",
    ):
        op.drop_table(table)

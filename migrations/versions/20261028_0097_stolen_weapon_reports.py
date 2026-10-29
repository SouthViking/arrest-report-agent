"""Persist stolen weapons and bucket evidence; configure current LSSD channels."""

import json
from collections.abc import Sequence
from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision: str = "20261028_0097"
down_revision: str | None = "20261028_0096"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "stolen_weapon_reports",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("submission_id", sa.BigInteger(), nullable=False, unique=True),
        sa.Column(
            "guild_id",
            sa.BigInteger(),
            sa.ForeignKey("discord_guilds.discord_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source_channel_id", sa.BigInteger(), nullable=False),
        sa.Column("destination_channel_id", sa.BigInteger(), nullable=False),
        sa.Column("registered_by", sa.BigInteger(), nullable=False),
        sa.Column("serial", sa.String(100), nullable=False),
        sa.Column("normalized_serial", sa.String(100), nullable=False),
        sa.Column("description", sa.String(2000), nullable=False),
        sa.Column("stolen_on", sa.Date(), nullable=False),
        sa.Column("victim_name", sa.String(100), nullable=False),
        sa.Column("reporting_officer_name", sa.String(100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("publication_status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("message_id", sa.BigInteger()),
        sa.CheckConstraint(
            "publication_status IN ('pending','publishing','published','failed')",
            name="ck_stolen_weapon_publication_status",
        ),
        sa.UniqueConstraint(
            "guild_id", "destination_channel_id", "message_id", name="uq_stolen_weapon_message"
        ),
    )
    op.create_index(
        "ix_stolen_weapon_guild_serial",
        "stolen_weapon_reports",
        ["guild_id", "normalized_serial", "created_at"],
    )
    op.create_table(
        "stolen_weapon_assets",
        sa.Column(
            "report_id",
            sa.Uuid(),
            sa.ForeignKey("stolen_weapon_reports.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("position", sa.SmallInteger(), primary_key=True),
        sa.Column("object_key", sa.String(512), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("content_type", sa.String(100), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("position BETWEEN 0 AND 4", name="ck_stolen_weapon_asset_position"),
        sa.CheckConstraint("size_bytes > 0", name="ck_stolen_weapon_asset_size"),
    )
    connection = op.get_bind()
    for row in connection.execute(
        sa.text(
            "SELECT revision.id, EXISTS (SELECT 1 FROM guild_access_policies AS policy "
            "JOIN guild_access_policy_members AS member ON member.policy_id = policy.id "
            "WHERE policy.config_revision_id = revision.id AND policy.mode = 'restricted' "
            "AND policy.policy_key = 'operational_staff_roles' AND member.snowflake_id > 0) AS can_enable "
            "FROM guild_config_revisions AS revision WHERE revision.guild_id = 1234674521416663070 "
            "AND revision.status IN ('draft', 'published')"
        )
    ).mappings():
        connection.execute(
            sa.text(
                "INSERT INTO guild_feature_settings "
                "(id, config_revision_id, feature_key, enabled, schema_version, payload) "
                "VALUES (:id, :revision, 'stolen_weapons', :enabled, 1, CAST(:payload AS jsonb)) "
                "ON CONFLICT (config_revision_id, feature_key) DO NOTHING"
            ),
            {
                "id": uuid4(),
                "revision": row["id"],
                "enabled": row["can_enable"],
                "payload": json.dumps(
                    {
                        "source_channel_ids": [1421923642983317545],
                        "destination_channel_id": 1565184787591860345,
                    }
                ),
            },
        )


def downgrade() -> None:
    op.get_bind().execute(sa.text("DELETE FROM guild_feature_settings WHERE feature_key = 'stolen_weapons'"))
    op.drop_table("stolen_weapon_assets")
    op.drop_index("ix_stolen_weapon_guild_serial", table_name="stolen_weapon_reports")
    op.drop_table("stolen_weapon_reports")

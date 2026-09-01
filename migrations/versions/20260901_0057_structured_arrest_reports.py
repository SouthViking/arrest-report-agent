"""Persist structured arrest report submissions and categorized assets."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260901_0057"
down_revision: str | None = "20260831_0056"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LSSD_GUILD_ID = 1234674521416663070
LSSD_REPORT_CHANNEL_ID = 1529616211204378737


def upgrade() -> None:
    op.create_table(
        "arrest_report_submissions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
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
        sa.Column("sarp_arrest_id", sa.BigInteger, nullable=False),
        sa.Column("suspect_name", sa.String(length=100), nullable=False),
        sa.Column("publication_status", sa.String(length=20), nullable=False),
        sa.Column("initial_review_status", sa.String(length=20), nullable=False),
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
        sa.CheckConstraint("sarp_arrest_id > 0", name="ck_arrest_submission_report_positive"),
        sa.CheckConstraint(
            "publication_status IN ('publishing','published','failed')",
            name="ck_arrest_submission_publication_status",
        ),
        sa.CheckConstraint(
            "initial_review_status IN ('pending','running','completed','failed')",
            name="ck_arrest_submission_review_status",
        ),
        sa.UniqueConstraint("guild_id", "sarp_arrest_id", name="uq_arrest_submission_guild_report"),
        sa.UniqueConstraint("guild_id", "root_message_id", name="uq_arrest_submission_guild_root"),
    )
    op.create_index(
        "ix_arrest_submission_thread",
        "arrest_report_submissions",
        ["guild_id", "thread_id"],
    )
    op.create_table(
        "arrest_report_assets",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "submission_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("arrest_report_submissions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("category", sa.String(length=30), nullable=False),
        sa.Column("position", sa.Integer, nullable=False),
        sa.Column("object_key", sa.String(length=500), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("content_type", sa.String(length=100), nullable=False),
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

    # Add the newly authorized LSSD report channel to the active revision without embedding
    # channel knowledge in runtime code. Existing configured channels remain untouched.
    connection = op.get_bind()
    policy = (
        connection.execute(
            sa.text(
                """
            SELECT policy.id
            FROM guild_access_policies AS policy
            JOIN discord_guilds AS guild
              ON guild.active_config_revision_id = policy.config_revision_id
            WHERE guild.discord_id = :guild_id
              AND policy.policy_key = 'report_channels'
              AND policy.mode = 'restricted'
            """
            ),
            {"guild_id": LSSD_GUILD_ID},
        )
        .mappings()
        .one_or_none()
    )
    if policy is not None:
        connection.execute(
            sa.text(
                "INSERT INTO guild_access_policy_members "
                "(id, policy_id, snowflake_id, position) "
                "SELECT :id, :policy_id, :channel_id, "
                "COALESCE(MAX(position), -1) + 1 "
                "FROM guild_access_policy_members WHERE policy_id = :policy_id "
                "ON CONFLICT (policy_id, snowflake_id) DO NOTHING"
            ),
            {
                "id": uuid.uuid4(),
                "policy_id": policy["id"],
                "channel_id": LSSD_REPORT_CHANNEL_ID,
            },
        )


def downgrade() -> None:
    op.drop_table("arrest_report_assets")
    op.drop_index("ix_arrest_submission_thread", table_name="arrest_report_submissions")
    op.drop_table("arrest_report_submissions")

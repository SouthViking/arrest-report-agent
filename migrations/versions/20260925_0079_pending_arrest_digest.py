"""Schedule pending-arrest digests and remember delivered reminders."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260925_0079"
down_revision: str | None = "20260924_0078"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LSSD_GUILD_ID = 1234674521416663070
COMMANDS_CHANNEL_ID = 1537203701893169254


def upgrade() -> None:
    op.create_table(
        "pending_arrest_digest_state",
        sa.Column(
            "guild_id",
            sa.BigInteger(),
            sa.ForeignKey("discord_guilds.discord_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("next_due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cycle_id", postgresql.UUID(as_uuid=True)),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("last_completed_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "pending_arrest_digest_deliveries",
        sa.Column(
            "guild_id",
            sa.BigInteger(),
            sa.ForeignKey("discord_guilds.discord_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("cycle_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("channel_id", sa.BigInteger(), primary_key=True),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("reply_sent", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "pending_arrest_digest_mentions",
        sa.Column(
            "guild_id",
            sa.BigInteger(),
            sa.ForeignKey("discord_guilds.discord_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("report_id", sa.BigInteger(), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), primary_key=True),
        sa.Column("stage", sa.SmallInteger(), nullable=False),
        sa.Column("mentioned_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("stage IN (1,2)", name="ck_pending_digest_mention_stage"),
    )
    op.get_bind().execute(
        sa.text(
            """
            UPDATE guild_feature_settings AS feature
            SET payload = feature.payload || jsonb_build_object(
                'digest_interval_hours', 6,
                'digest_lookback_days', 7,
                'digest_targets', jsonb_build_array(
                    jsonb_build_object('channel_id', CAST(:channel_id AS bigint), 'mention_users', true)
                )
            )
            FROM guild_config_revisions AS revision
            WHERE revision.id = feature.config_revision_id
              AND revision.guild_id = :guild_id
              AND feature.feature_key = 'pending_arrest_reports'
              AND NOT feature.payload ? 'digest_targets'
            """
        ),
        {"guild_id": LSSD_GUILD_ID, "channel_id": COMMANDS_CHANNEL_ID},
    )


def downgrade() -> None:
    op.get_bind().execute(
        sa.text(
            """
            UPDATE guild_feature_settings AS feature
            SET payload = feature.payload
                - 'digest_interval_hours' - 'digest_lookback_days' - 'digest_targets'
            FROM guild_config_revisions AS revision
            WHERE revision.id = feature.config_revision_id
              AND revision.guild_id = :guild_id
              AND feature.feature_key = 'pending_arrest_reports'
            """
        ),
        {"guild_id": LSSD_GUILD_ID},
    )
    op.drop_table("pending_arrest_digest_mentions")
    op.drop_table("pending_arrest_digest_deliveries")
    op.drop_table("pending_arrest_digest_state")

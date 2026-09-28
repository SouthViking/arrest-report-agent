"""Track and route report-owner quota exhaustion notifications."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260928_0083"
down_revision: str | None = "20260928_0082"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LSSD_GUILD_ID = 1234674521416663070
COMMAND_STAFF_CHANNEL_ID = 1261076248541532240


def upgrade() -> None:
    op.create_table(
        "report_owner_quota_exhaustion_notifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "guild_id",
            sa.BigInteger(),
            sa.ForeignKey("discord_guilds.discord_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("root_message_id", sa.BigInteger(), nullable=False),
        sa.Column("owner_user_id", sa.BigInteger(), nullable=False),
        sa.Column("max_rounds", sa.Integer(), nullable=False),
        sa.Column("message_id", sa.BigInteger()),
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
    op.get_bind().execute(
        sa.text(
            """
            UPDATE guild_feature_settings AS feature
            SET payload = feature.payload || jsonb_build_object(
                'quota_exhausted_notification_channel_id', CAST(:channel_id AS bigint)
            )
            FROM guild_config_revisions AS revision
            WHERE revision.id = feature.config_revision_id
              AND revision.guild_id = :guild_id
              AND feature.feature_key = 'report_owner_access'
              AND NOT feature.payload ? 'quota_exhausted_notification_channel_id'
            """
        ),
        {"guild_id": LSSD_GUILD_ID, "channel_id": COMMAND_STAFF_CHANNEL_ID},
    )


def downgrade() -> None:
    op.get_bind().execute(
        sa.text(
            """
            UPDATE guild_feature_settings AS feature
            SET payload = feature.payload - 'quota_exhausted_notification_channel_id'
            FROM guild_config_revisions AS revision
            WHERE revision.id = feature.config_revision_id
              AND revision.guild_id = :guild_id
              AND feature.feature_key = 'report_owner_access'
            """
        ),
        {"guild_id": LSSD_GUILD_ID},
    )
    op.drop_table("report_owner_quota_exhaustion_notifications")

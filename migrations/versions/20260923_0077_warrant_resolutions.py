"""Route warrant notices and persist their resolution state."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260923_0077"
down_revision: str | None = "20260923_0076"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LSSD_GUILD_ID = 1234674521416663070
NOTICE_CHANNEL_ID = 1552367150591905793
RESULT_CHANNEL_ID = 1537203701893169254
PREVIOUS_CHANNEL_ID = 1261076248541532240


def upgrade() -> None:
    op.create_table(
        "warrant_notifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "guild_id",
            sa.BigInteger(),
            sa.ForeignKey("discord_guilds.discord_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("interaction_id", sa.BigInteger(), nullable=False, unique=True),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger()),
        sa.Column("requester_user_id", sa.BigInteger(), nullable=False),
        sa.Column("report_url", sa.String(length=1000), nullable=False),
        sa.Column("warrant_id", sa.BigInteger(), nullable=False),
        sa.Column("warrant_type", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("resolved_by_user_id", sa.BigInteger()),
        sa.Column("rejection_reason", sa.String(length=500)),
        sa.Column("result_message_id", sa.BigInteger()),
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
    )
    op.create_index(
        "ix_warrant_notification_guild_status",
        "warrant_notifications",
        ["guild_id", "status", "created_at"],
    )
    op.execute(
        sa.text(
            """
            UPDATE guild_feature_settings AS feature
            SET payload = feature.payload || jsonb_build_object(
                'channel_id', CAST(:channel_id AS bigint),
                'result_channel_id', CAST(:result_channel_id AS bigint)
            )
            FROM guild_config_revisions AS revision
            WHERE revision.id = feature.config_revision_id
              AND revision.guild_id = :guild_id
              AND revision.status IN ('draft', 'published')
              AND feature.feature_key = 'warrant_notifications'
              AND feature.payload ->> 'channel_id' = :previous_channel_id
            """
        ).bindparams(
            guild_id=LSSD_GUILD_ID,
            channel_id=NOTICE_CHANNEL_ID,
            result_channel_id=RESULT_CHANNEL_ID,
            previous_channel_id=str(PREVIOUS_CHANNEL_ID),
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE guild_feature_settings AS feature
            SET payload = jsonb_set(feature.payload - 'result_channel_id',
                                    '{channel_id}', CAST(:previous_channel_id AS jsonb))
            FROM guild_config_revisions AS revision
            WHERE revision.id = feature.config_revision_id
              AND revision.guild_id = :guild_id
              AND revision.status IN ('draft', 'published')
              AND feature.feature_key = 'warrant_notifications'
              AND feature.payload ->> 'channel_id' = :channel_id
            """
        ).bindparams(
            guild_id=LSSD_GUILD_ID,
            channel_id=str(NOTICE_CHANNEL_ID),
            previous_channel_id=str(PREVIOUS_CHANNEL_ID),
        )
    )
    op.drop_index("ix_warrant_notification_guild_status", table_name="warrant_notifications")
    op.drop_table("warrant_notifications")

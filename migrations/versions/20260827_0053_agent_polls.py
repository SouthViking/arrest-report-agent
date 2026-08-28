"""Add minimal metadata index for native agent polls."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260827_0053"
down_revision: str | None = "20260826_0052"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_polls",
        sa.Column("message_id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "guild_id",
            sa.BigInteger(),
            sa.ForeignKey("discord_guilds.discord_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("creator_user_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column("last_synced_at", sa.DateTime(timezone=True)),
        sa.Column("last_activity_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "expires_at > created_at",
            name="ck_agent_poll_expiry_after_creation",
        ),
    )
    op.create_index(
        "ix_agent_poll_guild_created",
        "agent_polls",
        ["guild_id", "created_at"],
    )
    op.create_index(
        "ix_agent_poll_creator_created",
        "agent_polls",
        ["guild_id", "creator_user_id", "created_at"],
    )
    op.create_index(
        "ix_agent_poll_channel_created",
        "agent_polls",
        ["guild_id", "channel_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_agent_poll_channel_created", table_name="agent_polls")
    op.drop_index("ix_agent_poll_creator_created", table_name="agent_polls")
    op.drop_index("ix_agent_poll_guild_created", table_name="agent_polls")
    op.drop_table("agent_polls")

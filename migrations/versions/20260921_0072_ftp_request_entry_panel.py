"""Add the FTP request entry panel and allow concurrent requests per trainee."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260921_0072"
down_revision: str | None = "20260921_0071"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index(
        "uq_ftp_training_request_live_member",
        table_name="ftp_training_requests",
    )
    op.create_table(
        "ftp_request_panels",
        sa.Column(
            "guild_id",
            sa.BigInteger(),
            sa.ForeignKey("discord_guilds.discord_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
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
        sa.UniqueConstraint(
            "guild_id",
            "channel_id",
            "message_id",
            name="uq_ftp_request_panel_message",
        ),
    )


def downgrade() -> None:
    op.drop_table("ftp_request_panels")
    op.create_index(
        "uq_ftp_training_request_live_member",
        "ftp_training_requests",
        ["guild_id", "requester_user_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('publishing','pending')"),
    )

"""Allow audited extra report-owner review rounds."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260924_0078"
down_revision: str | None = "20260923_0077"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "report_owner_round_grants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "guild_id",
            sa.BigInteger(),
            sa.ForeignKey("discord_guilds.discord_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("root_message_id", sa.BigInteger(), nullable=False),
        sa.Column("owner_user_id", sa.BigInteger(), nullable=False),
        sa.Column("granted_by_user_id", sa.BigInteger(), nullable=False),
        sa.Column("interaction_id", sa.BigInteger(), nullable=False, unique=True),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("amount BETWEEN 1 AND 10", name="ck_report_owner_round_grant_amount"),
    )
    op.create_index(
        "ix_report_owner_round_grant_usage",
        "report_owner_round_grants",
        ["guild_id", "root_message_id", "owner_user_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_report_owner_round_grant_usage", table_name="report_owner_round_grants")
    op.drop_table("report_owner_round_grants")

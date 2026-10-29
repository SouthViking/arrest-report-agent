"""Keep audited theft closures and deduplicate internal review alerts."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261029_0098"
down_revision: str | None = "20261028_0097"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "stolen_weapon_reports", sa.Column("status", sa.String(12), nullable=False, server_default="open")
    )
    op.add_column("stolen_weapon_reports", sa.Column("closed_by", sa.BigInteger()))
    op.add_column("stolen_weapon_reports", sa.Column("closed_at", sa.DateTime(timezone=True)))
    op.add_column("stolen_weapon_reports", sa.Column("closure_reason", sa.String(500)))
    op.add_column("stolen_weapon_reports", sa.Column("closure_interaction_id", sa.BigInteger()))
    op.create_unique_constraint(
        "uq_stolen_weapon_closure_interaction", "stolen_weapon_reports", ["closure_interaction_id"]
    )
    op.create_check_constraint(
        "ck_stolen_weapon_closure",
        "stolen_weapon_reports",
        "(status = 'open' AND closed_by IS NULL AND closed_at IS NULL "
        "AND closure_reason IS NULL AND closure_interaction_id IS NULL) OR "
        "(status = 'closed' AND closed_by IS NOT NULL AND closed_at IS NOT NULL "
        "AND closure_reason IS NOT NULL AND closure_interaction_id IS NOT NULL)",
    )
    op.create_table(
        "weapon_theft_alerts",
        sa.Column(
            "report_id",
            sa.Uuid(),
            sa.ForeignKey("stolen_weapon_reports.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("review_root_message_id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "guild_id",
            sa.BigInteger(),
            sa.ForeignKey("discord_guilds.discord_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger()),
        sa.Column("evidence_ids", postgresql.ARRAY(sa.BigInteger()), nullable=False),
        sa.Column("status", sa.String(12), nullable=False, server_default="sending"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('sending','sent','failed','unknown')", name="ck_weapon_theft_alert_status"
        ),
    )


def downgrade() -> None:
    op.drop_table("weapon_theft_alerts")
    op.drop_constraint("ck_stolen_weapon_closure", "stolen_weapon_reports", type_="check")
    op.drop_constraint("uq_stolen_weapon_closure_interaction", "stolen_weapon_reports", type_="unique")
    for column in ("closure_interaction_id", "closure_reason", "closed_at", "closed_by", "status"):
        op.drop_column("stolen_weapon_reports", column)

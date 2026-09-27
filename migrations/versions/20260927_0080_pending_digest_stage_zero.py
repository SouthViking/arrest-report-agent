"""Allow on-time pending mentions and stop retrying an already delivered reply."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260927_0080"
down_revision: str | None = "20260925_0079"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LSSD_GUILD_ID = 1234674521416663070
COMMANDS_CHANNEL_ID = 1537203701893169254


def upgrade() -> None:
    op.drop_constraint(
        "ck_pending_digest_mention_stage", "pending_arrest_digest_mentions", type_="check"
    )
    op.create_check_constraint(
        "ck_pending_digest_mention_stage", "pending_arrest_digest_mentions", "stage IN (0,1,2)"
    )
    # The current commands-channel cycle has already posted its reply, but the old
    # constraint prevented reply_sent from being saved. Do not repeat it on restart.
    op.get_bind().execute(
        sa.text(
            """
            UPDATE pending_arrest_digest_deliveries AS delivery
            SET reply_sent = TRUE
            FROM pending_arrest_digest_state AS state
            WHERE delivery.guild_id = state.guild_id
              AND delivery.cycle_id = state.cycle_id
              AND state.cycle_id IS NOT NULL
              AND delivery.guild_id = :guild_id
              AND delivery.channel_id = :channel_id
              AND delivery.reply_sent = FALSE
            """
        ),
        {"guild_id": LSSD_GUILD_ID, "channel_id": COMMANDS_CHANNEL_ID},
    )


def downgrade() -> None:
    op.get_bind().execute(
        sa.text("UPDATE pending_arrest_digest_mentions SET stage = 1 WHERE stage = 0")
    )
    op.drop_constraint(
        "ck_pending_digest_mention_stage", "pending_arrest_digest_mentions", type_="check"
    )
    op.create_check_constraint(
        "ck_pending_digest_mention_stage", "pending_arrest_digest_mentions", "stage IN (1,2)"
    )

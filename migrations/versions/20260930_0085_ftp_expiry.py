"""Persist automatic FTP cancellation and retryable Discord synchronization."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260930_0085"
down_revision: str | None = "20260929_0084"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("ftp_training_requests", sa.Column("auto_cancelled_at", sa.DateTime(timezone=True)))
    op.add_column("ftp_training_requests", sa.Column("expiry_sync_due_at", sa.DateTime(timezone=True)))
    op.create_check_constraint(
        "ck_ftp_auto_cancellation", "ftp_training_requests",
        "(auto_cancelled_at IS NULL AND expiry_sync_due_at IS NULL) OR "
        "(auto_cancelled_at IS NOT NULL AND status = 'cancelled')",
    )
    op.create_index(
        "ix_ftp_pending_expiry", "ftp_training_requests",
        ["guild_id", sa.text("COALESCE(scheduled_for, created_at)")],
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_ftp_expiry_sync", "ftp_training_requests", ["guild_id", "expiry_sync_due_at"],
        postgresql_where=sa.text("expiry_sync_due_at IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_ftp_expiry_sync", table_name="ftp_training_requests")
    op.drop_index("ix_ftp_pending_expiry", table_name="ftp_training_requests")
    op.drop_constraint("ck_ftp_auto_cancellation", "ftp_training_requests", type_="check")
    op.drop_column("ftp_training_requests", "expiry_sync_due_at")
    op.drop_column("ftp_training_requests", "auto_cancelled_at")

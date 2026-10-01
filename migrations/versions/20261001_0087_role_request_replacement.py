"""Replace pending role requests atomically and retain cancellation delivery state."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261001_0087"
down_revision: str | None = "20260930_0086"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("role_requests", sa.Column("replaced_by_request_id", postgresql.UUID(as_uuid=True)))
    op.create_foreign_key("fk_role_request_replacement", "role_requests", "role_requests",
                          ["replaced_by_request_id"], ["id"], ondelete="SET NULL")
    op.add_column("role_requests", sa.Column("cancellation_sync_due", sa.DateTime(timezone=True)))
    op.drop_constraint("ck_role_request_status", "role_requests", type_="check")
    op.create_check_constraint("ck_role_request_status", "role_requests",
                               "status IN ('publishing','pending','processing','approved',"
                               "'partially_approved','rejected','failed','cancelled')")
    # A not-yet-published replacement must not discard the current request on failure.
    op.drop_index("uq_role_request_live_requester", table_name="role_requests")
    op.create_index("uq_role_request_live_requester", "role_requests", ["guild_id", "requester_id"],
                    unique=True, postgresql_where=sa.text("status IN ('pending','processing')"))
    op.create_index("uq_role_request_publishing_requester", "role_requests", ["guild_id", "requester_id"],
                    unique=True, postgresql_where=sa.text("status = 'publishing'"))
    op.create_index("ix_role_request_cancellation_sync", "role_requests", ["cancellation_sync_due"],
                    postgresql_where=sa.text("cancellation_sync_due IS NOT NULL"))


def downgrade() -> None:
    # Preserve the history using an old terminal status. Do not discard pending requests.
    op.execute("UPDATE role_requests SET status='failed' WHERE status='cancelled'")
    op.execute("""
        UPDATE role_requests draft SET status='failed'
        WHERE draft.status='publishing' AND EXISTS (
            SELECT 1 FROM role_requests current
            WHERE current.guild_id=draft.guild_id AND current.requester_id=draft.requester_id
              AND current.status IN ('pending','processing')
        )
    """)
    op.drop_index("ix_role_request_cancellation_sync", table_name="role_requests")
    op.drop_index("uq_role_request_publishing_requester", table_name="role_requests")
    op.drop_index("uq_role_request_live_requester", table_name="role_requests")
    op.create_index("uq_role_request_live_requester", "role_requests", ["guild_id", "requester_id"],
                    unique=True, postgresql_where=sa.text("status IN ('publishing','pending','processing')"))
    op.drop_constraint("ck_role_request_status", "role_requests", type_="check")
    op.create_check_constraint("ck_role_request_status", "role_requests",
                               "status IN ('publishing','pending','processing','approved',"
                               "'partially_approved','rejected','failed')")
    op.drop_column("role_requests", "cancellation_sync_due")
    op.drop_constraint("fk_role_request_replacement", "role_requests", type_="foreignkey")
    op.drop_column("role_requests", "replaced_by_request_id")

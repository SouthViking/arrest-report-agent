"""Persist generic Agents SDK conversation sessions in PostgreSQL."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260831_0056"
down_revision: str | None = "20260831_0055"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agent_sessions",
        sa.Column("session_id", sa.String(length=70), primary_key=True),
        sa.Column("namespace", sa.String(length=80), nullable=False),
        sa.Column("scope_key", sa.String(length=255), nullable=False),
        sa.Column("access_partition", sa.String(length=80), nullable=False),
        sa.Column("source_cursor", sa.String(length=255)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=False),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=False),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "namespace",
            "scope_key",
            "access_partition",
            name="uq_agent_session_scope",
        ),
    )
    op.create_table(
        "agent_messages",
        sa.Column("id", sa.Integer(), autoincrement=True, primary_key=True),
        sa.Column(
            "session_id",
            sa.String(length=70),
            sa.ForeignKey("agent_sessions.session_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("message_data", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=False),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "idx_agent_messages_session_time",
        "agent_messages",
        ["session_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("idx_agent_messages_session_time", table_name="agent_messages")
    op.drop_table("agent_messages")
    op.drop_table("agent_sessions")

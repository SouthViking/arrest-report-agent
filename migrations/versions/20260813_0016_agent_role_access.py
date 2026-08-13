"""Allow the Discord agent through an independent role policy."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260813_0016"
down_revision: str | None = "20260813_0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

POLICY_KEY = "agent_roles"


def upgrade() -> None:
    connection = op.get_bind()
    revision_ids = connection.execute(sa.text("SELECT id FROM guild_config_revisions")).scalars()
    policy_table = sa.table(
        "guild_access_policies",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("config_revision_id", postgresql.UUID(as_uuid=True)),
        sa.column("policy_key", sa.String()),
        sa.column("subject_type", sa.String()),
        sa.column("mode", sa.String()),
    )
    rows = [
        {
            "id": uuid.uuid4(),
            "config_revision_id": revision_id,
            "policy_key": POLICY_KEY,
            "subject_type": "role",
            "mode": "disabled",
        }
        for revision_id in revision_ids
    ]
    if rows:
        op.bulk_insert(policy_table, rows)


def downgrade() -> None:
    op.execute(f"DELETE FROM guild_access_policies WHERE policy_key = '{POLICY_KEY}'")

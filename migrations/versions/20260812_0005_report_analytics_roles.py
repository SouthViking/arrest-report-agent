"""Restrict public report analytics to dedicated staff roles."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260812_0005"
down_revision: str | None = "20260811_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

POLICY_KEY = "report_analytics_roles"
SOURCE_POLICIES = ("pager_creator_roles", "supervisor_roles")


def upgrade() -> None:
    connection = op.get_bind()
    revisions = connection.execute(sa.text("SELECT id FROM guild_config_revisions")).scalars().all()
    policy_table = sa.table(
        "guild_access_policies",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("config_revision_id", postgresql.UUID(as_uuid=True)),
        sa.column("policy_key", sa.String()),
        sa.column("subject_type", sa.String()),
        sa.column("mode", sa.String()),
    )
    member_table = sa.table(
        "guild_access_policy_members",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("policy_id", postgresql.UUID(as_uuid=True)),
        sa.column("snowflake_id", sa.BigInteger()),
    )

    for config_revision_id in revisions:
        role_ids = tuple(
            int(role_id)
            for role_id in connection.execute(
                sa.text(
                    "SELECT DISTINCT member.snowflake_id "
                    "FROM guild_access_policies AS policy "
                    "JOIN guild_access_policy_members AS member ON member.policy_id = policy.id "
                    "WHERE policy.config_revision_id = :revision_id "
                    "AND policy.policy_key IN :source_policies "
                    "AND policy.mode = 'restricted' "
                    "ORDER BY member.snowflake_id"
                ).bindparams(sa.bindparam("source_policies", expanding=True)),
                {
                    "revision_id": config_revision_id,
                    "source_policies": SOURCE_POLICIES,
                },
            ).scalars()
        )
        policy_id = uuid.uuid4()
        op.bulk_insert(
            policy_table,
            [
                {
                    "id": policy_id,
                    "config_revision_id": config_revision_id,
                    "policy_key": POLICY_KEY,
                    "subject_type": "role",
                    "mode": "restricted" if role_ids else "disabled",
                }
            ],
        )
        if role_ids:
            op.bulk_insert(
                member_table,
                [
                    {
                        "id": uuid.uuid4(),
                        "policy_id": policy_id,
                        "snowflake_id": role_id,
                    }
                    for role_id in role_ids
                ],
            )


def downgrade() -> None:
    op.execute(f"DELETE FROM guild_access_policies WHERE policy_key = '{POLICY_KEY}'")

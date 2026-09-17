"""Authorize LSSD Executive Staff to review role requests."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260917_0067"
down_revision: str | None = "20260916_0066"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LSSD_GUILD_ID = 1234674521416663070
EXECUTIVE_STAFF_ROLE_ID = 1234678238182244414
REVIEWER_ROLES_POLICY_KEY = "role_request_reviewer_roles"


def upgrade() -> None:
    """Append Executive Staff to every restricted LSSD reviewer policy."""

    connection = op.get_bind()
    policy_ids = connection.execute(
        sa.text(
            """
            SELECT policy.id
            FROM guild_access_policies AS policy
            JOIN guild_config_revisions AS revision
              ON revision.id = policy.config_revision_id
            WHERE revision.guild_id = :guild_id
              AND policy.policy_key = :policy_key
              AND policy.mode = 'restricted'
            """
        ),
        {
            "guild_id": LSSD_GUILD_ID,
            "policy_key": REVIEWER_ROLES_POLICY_KEY,
        },
    ).scalars().all()

    for policy_id in policy_ids:
        connection.execute(
            sa.text(
                "INSERT INTO guild_access_policy_members "
                "(id, policy_id, snowflake_id, position) "
                "SELECT :id, :policy_id, :role_id, COALESCE(MAX(position), -1) + 1 "
                "FROM guild_access_policy_members WHERE policy_id = :policy_id "
                "ON CONFLICT (policy_id, snowflake_id) DO NOTHING"
            ),
            {
                "id": uuid.uuid4(),
                "policy_id": policy_id,
                "role_id": EXECUTIVE_STAFF_ROLE_ID,
            },
        )


def downgrade() -> None:
    """Keep the explicitly authorized reviewer when rolling application code back."""

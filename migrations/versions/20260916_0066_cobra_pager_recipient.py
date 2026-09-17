"""Add the DLM COBRA Team role to the LSSD pager recipients."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260916_0066"
down_revision: str | None = "20260913_0065"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LSSD_GUILD_ID = 1234674521416663070
COBRA_TEAM_ROLE_ID = 1326740806656069737
PAGER_RECIPIENT_POLICY_KEY = "pager_recipient_roles"


def upgrade() -> None:
    """Append COBRA Team without replacing the existing pager recipient catalog."""

    connection = op.get_bind()
    policy_id = connection.execute(
        sa.text(
            """
            SELECT policy.id
            FROM guild_access_policies AS policy
            JOIN discord_guilds AS guild
              ON guild.active_config_revision_id = policy.config_revision_id
            WHERE guild.discord_id = :guild_id
              AND policy.policy_key = :policy_key
              AND policy.mode = 'restricted'
            """
        ),
        {
            "guild_id": LSSD_GUILD_ID,
            "policy_key": PAGER_RECIPIENT_POLICY_KEY,
        },
    ).scalar_one_or_none()
    if policy_id is None:
        return

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
            "role_id": COBRA_TEAM_ROLE_ID,
        },
    )


def downgrade() -> None:
    """Keep the explicitly authorized recipient when rolling application code back."""


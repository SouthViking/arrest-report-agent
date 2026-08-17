"""Separate Railway deployment incidents from public release channels."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260817_0028"
down_revision: str | None = "20260815_0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

POLICY_KEY = "deployment_incident_notification_channels"
LSSD_GUILD_ID = 1234674521416663070
LSSD_BOT_GIT_CHANNEL_ID = 1241206587079524442


def upgrade() -> None:
    connection = op.get_bind()
    revisions = (
        connection.execute(
            sa.text(
                "SELECT id, guild_id FROM guild_config_revisions revision "
                "WHERE NOT EXISTS ("
                "SELECT 1 FROM guild_access_policies policy "
                "WHERE policy.config_revision_id = revision.id "
                "AND policy.policy_key = :policy_key)"
            ),
            {"policy_key": POLICY_KEY},
        )
        .mappings()
        .all()
    )
    for config_revision in revisions:
        policy_id = uuid.uuid4()
        is_lssd = int(config_revision["guild_id"]) == LSSD_GUILD_ID
        connection.execute(
            sa.text(
                "INSERT INTO guild_access_policies "
                "(id, config_revision_id, policy_key, subject_type, mode) "
                "VALUES (:id, :revision_id, :policy_key, 'channel', :mode)"
            ),
            {
                "id": policy_id,
                "revision_id": config_revision["id"],
                "policy_key": POLICY_KEY,
                "mode": "restricted" if is_lssd else "disabled",
            },
        )
        if is_lssd:
            connection.execute(
                sa.text(
                    "INSERT INTO guild_access_policy_members "
                    "(id, policy_id, snowflake_id, position) "
                    "VALUES (:id, :policy_id, :channel_id, 0)"
                ),
                {
                    "id": uuid.uuid4(),
                    "policy_id": policy_id,
                    "channel_id": LSSD_BOT_GIT_CHANNEL_ID,
                },
            )


def downgrade() -> None:
    op.get_bind().execute(
        sa.text("DELETE FROM guild_access_policies WHERE policy_key = :policy_key"),
        {"policy_key": POLICY_KEY},
    )

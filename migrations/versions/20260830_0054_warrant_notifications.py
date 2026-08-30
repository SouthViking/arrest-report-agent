"""Configure warrant notifications for operational staff."""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260830_0054"
down_revision: str | None = "20260827_0053"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FEATURE_KEY = "warrant_notifications"
LSSD_GUILD_ID = 1234674521416663070
LSSD_OPTIONS = {
    "channel_id": 1261076248541532240,
    "command_role_ids": [1234678029662294047],
    "executive_role_ids": [1234678238182244414],
}


def upgrade() -> None:
    connection = op.get_bind()
    revisions = connection.execute(
        sa.text("SELECT id, guild_id FROM guild_config_revisions")
    ).mappings().all()
    for config_revision in revisions:
        enabled = int(config_revision["guild_id"]) == LSSD_GUILD_ID
        connection.execute(
            sa.text(
                "INSERT INTO guild_feature_settings "
                "(id, config_revision_id, feature_key, enabled, schema_version, payload) "
                "VALUES (:id, :revision_id, :key, :enabled, 1, CAST(:payload AS jsonb))"
            ),
            {
                "id": uuid.uuid4(),
                "revision_id": config_revision["id"],
                "key": FEATURE_KEY,
                "enabled": enabled,
                "payload": json.dumps(LSSD_OPTIONS if enabled else {}),
            },
        )


def downgrade() -> None:
    op.get_bind().execute(
        sa.text("DELETE FROM guild_feature_settings WHERE feature_key = :key"),
        {"key": FEATURE_KEY},
    )

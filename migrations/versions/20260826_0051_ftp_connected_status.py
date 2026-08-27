"""Add the connected Field Training Program summary."""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260826_0051"
down_revision: str | None = "20260825_0050"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FEATURE_KEY = "ftp_status"
ACCESS_POLICY_KEY = "operational_staff_roles"
LSSD_GUILD_ID = 1234674521416663070
LSSD_OPTIONS = {
    "faction_id": 3,
    "supervisory_role_ids": [1234676131240869949],
    "mfto_role_ids": [1234691474055233607],
    "fto_role_ids": [1234675643959083021],
    "ftt_role_ids": [1234675742013390849],
}


def upgrade() -> None:
    connection = op.get_bind()
    revisions = (
        connection.execute(sa.text("SELECT id, guild_id FROM guild_config_revisions")).mappings().all()
    )
    for config_revision in revisions:
        is_lssd = int(config_revision["guild_id"]) == LSSD_GUILD_ID
        enabled = is_lssd and _has_restricted_operational_roles(
            connection,
            config_revision["id"],
        )
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
    op.execute(
        sa.text("DELETE FROM guild_feature_settings WHERE feature_key = :key").bindparams(key=FEATURE_KEY)
    )


def _has_restricted_operational_roles(
    connection: sa.Connection,
    revision_id: object,
) -> bool:
    return bool(
        connection.execute(
            sa.text(
                "SELECT EXISTS ("
                "SELECT 1 FROM guild_access_policies policy "
                "JOIN guild_access_policy_members member ON member.policy_id = policy.id "
                "WHERE policy.config_revision_id = :revision_id "
                "AND policy.policy_key = :policy_key "
                "AND policy.mode = 'restricted'"
                ")"
            ),
            {"revision_id": revision_id, "policy_key": ACCESS_POLICY_KEY},
        ).scalar_one()
    )

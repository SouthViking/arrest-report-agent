"""Add deterministic radio queries and the restricted AI audit module."""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260823_0046"
down_revision: str | None = "20260821_0045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FEATURE_KEY = "radio_audit"
POLICY_KEY = "radio_audit_roles"
SOURCE_POLICY_KEY = "agent_roles"
LSSD_GUILD_ID = 1234674521416663070
LSSD_OPTIONS = {
    "faction_id": 3,
    "query_max_range_days": 30,
    "query_max_records": 2_000,
    "audit_max_range_days": 7,
    "audit_max_records": 500,
}


def upgrade() -> None:
    connection = op.get_bind()
    revisions = (
        connection.execute(sa.text("SELECT id, guild_id FROM guild_config_revisions")).mappings().all()
    )
    for config_revision in revisions:
        enabled = int(config_revision["guild_id"]) == LSSD_GUILD_ID
        role_ids = _source_role_ids(connection, config_revision["id"]) if enabled else ()
        policy_id = uuid.uuid4()
        connection.execute(
            sa.text(
                "INSERT INTO guild_access_policies "
                "(id, config_revision_id, policy_key, subject_type, mode) "
                "VALUES (:id, :revision_id, :key, 'role', :mode)"
            ),
            {
                "id": policy_id,
                "revision_id": config_revision["id"],
                "key": POLICY_KEY,
                "mode": "restricted" if role_ids else "disabled",
            },
        )
        for position, role_id in enumerate(role_ids):
            connection.execute(
                sa.text(
                    "INSERT INTO guild_access_policy_members "
                    "(id, policy_id, snowflake_id, position) "
                    "VALUES (:id, :policy_id, :role_id, :position)"
                ),
                {
                    "id": uuid.uuid4(),
                    "policy_id": policy_id,
                    "role_id": role_id,
                    "position": position,
                },
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
                "enabled": enabled and bool(role_ids),
                "payload": json.dumps(LSSD_OPTIONS if enabled and role_ids else {}),
            },
        )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text("DELETE FROM guild_feature_settings WHERE feature_key = :key"),
        {"key": FEATURE_KEY},
    )
    connection.execute(
        sa.text("DELETE FROM guild_access_policies WHERE policy_key = :key"),
        {"key": POLICY_KEY},
    )


def _source_role_ids(connection: sa.Connection, revision_id: object) -> tuple[int, ...]:
    return tuple(
        int(role_id)
        for role_id in connection.execute(
            sa.text(
                "SELECT member.snowflake_id "
                "FROM guild_access_policies policy "
                "JOIN guild_access_policy_members member ON member.policy_id = policy.id "
                "WHERE policy.config_revision_id = :revision_id "
                "AND policy.policy_key = :source_key "
                "AND policy.mode = 'restricted' "
                "ORDER BY member.position, member.snowflake_id"
            ),
            {"revision_id": revision_id, "source_key": SOURCE_POLICY_KEY},
        ).scalars()
    )

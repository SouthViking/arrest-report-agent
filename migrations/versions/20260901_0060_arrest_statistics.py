"""Add restricted deterministic arrest-charge statistics."""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260901_0060"
down_revision: str | None = "20260901_0059"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FEATURE_KEY = "arrest_statistics"
LSSD_GUILD_ID = 1234674521416663070
SOURCE_POLICY_KEY = "faction_report_roles"
LSSD_OPTIONS = {
    "faction_id": 3,
    "max_range_days": 30,
    "max_reports": 200,
    "cache_ttl_seconds": 900,
    "detail_concurrency": 3,
    "max_top": 10,
}


def upgrade() -> None:
    connection = op.get_bind()
    revisions = connection.execute(
        sa.text("SELECT id, guild_id FROM guild_config_revisions")
    ).mappings()
    for config_revision in revisions:
        enabled = (
            int(config_revision["guild_id"]) == LSSD_GUILD_ID
            and _has_restricted_roles(connection, config_revision["id"])
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
    connection = op.get_bind()
    connection.execute(
        sa.text("DELETE FROM guild_feature_settings WHERE feature_key = :key"),
        {"key": FEATURE_KEY},
    )


def _has_restricted_roles(connection: sa.Connection, revision_id: object) -> bool:
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
            {"revision_id": revision_id, "policy_key": SOURCE_POLICY_KEY},
        ).scalar_one()
    )

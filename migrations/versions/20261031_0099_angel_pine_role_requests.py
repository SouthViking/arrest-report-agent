"""Offer Angel Pine in LSSD FTT entry and manual role requests."""

import json
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261031_0099"
down_revision: str | None = "20261029_0098"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LSSD_GUILD_ID = 1234674521416663070
ANGEL_PINE_ROLE_ID = 1358795936033276029
ANGEL_PINE_STATION = {
    "key": "angel_pine",
    "label": "Angel Pine",
    "role_id": ANGEL_PINE_ROLE_ID,
    "emoji": "🌲",
}


def upgrade() -> None:
    connection = op.get_bind()
    policies = connection.execute(
        sa.text("""
        SELECT policy.id, policy.config_revision_id
        FROM guild_access_policies AS policy
        JOIN guild_config_revisions AS revision ON revision.id = policy.config_revision_id
        WHERE revision.guild_id = :guild_id AND revision.status IN ('draft', 'published')
          AND policy.policy_key = 'role_request_allowed_roles' AND policy.mode = 'restricted'
        """),
        {"guild_id": LSSD_GUILD_ID},
    ).mappings().all()
    for policy in policies:
        connection.execute(
            sa.text("""
            INSERT INTO guild_access_policy_members (id, policy_id, snowflake_id, position)
            SELECT :id, :policy_id, :role_id, COALESCE(MAX(position), -1) + 1
            FROM guild_access_policy_members WHERE policy_id = :policy_id
            ON CONFLICT (policy_id, snowflake_id) DO NOTHING
            """),
            {"id": uuid.uuid4(), "policy_id": policy["id"], "role_id": ANGEL_PINE_ROLE_ID},
        )
        feature = connection.execute(
            sa.text("""
            SELECT id, payload FROM guild_feature_settings
            WHERE config_revision_id = :revision_id AND feature_key = 'role_requests'
            """),
            {"revision_id": policy["config_revision_id"]},
        ).mappings().first()
        if feature is None:
            continue
        payload = dict(feature["payload"])
        entry = payload.get("ftt_entry")
        if not isinstance(entry, dict) or not isinstance(entry.get("stations"), list):
            continue
        stations = entry["stations"]
        if len(stations) >= 10 or any(
            station.get("key") == ANGEL_PINE_STATION["key"]
            or str(station.get("role_id")) == str(ANGEL_PINE_ROLE_ID)
            for station in stations
        ):
            continue
        payload["ftt_entry"] = {**entry, "stations": [*stations, ANGEL_PINE_STATION]}
        connection.execute(
            sa.text("UPDATE guild_feature_settings SET payload = CAST(:payload AS jsonb) WHERE id = :id"),
            {"id": feature["id"], "payload": json.dumps(payload)},
        )


def downgrade() -> None:
    """Preserve available stations, custom configuration and existing requests on rollback."""

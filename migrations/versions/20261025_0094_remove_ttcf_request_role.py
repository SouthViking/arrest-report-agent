"""Stop offering CSD TTCF in LSSD role requests and new FTT entry packages."""

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261025_0094"
down_revision: str | None = "20261022_0093"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LSSD_GUILD_ID = 1234674521416663070
RETIRED_ROLE_ID = 1241871524424712314


def upgrade() -> None:
    connection = op.get_bind()
    parameters = {"guild_id": LSSD_GUILD_ID, "role_id": RETIRED_ROLE_ID}
    features = connection.execute(
        sa.text("""
        SELECT feature.id, feature.payload
        FROM guild_feature_settings AS feature
        JOIN guild_config_revisions AS revision ON revision.id = feature.config_revision_id
        WHERE revision.guild_id = :guild_id AND revision.status IN ('draft', 'published')
          AND feature.feature_key = 'role_requests'
        """),
        {"guild_id": LSSD_GUILD_ID},
    ).mappings().all()
    for feature in features:
        payload = dict(feature["payload"])
        entry = payload.get("ftt_entry")
        if not isinstance(entry, dict) or not isinstance(entry.get("base_role_ids"), list):
            continue
        base_ids = [role_id for role_id in entry["base_role_ids"] if str(role_id) != str(RETIRED_ROLE_ID)]
        if base_ids == entry["base_role_ids"]:
            continue
        if base_ids:
            payload["ftt_entry"] = {**entry, "base_role_ids": base_ids}
        else:
            # A custom package containing only the withdrawn role is no longer an entry package.
            payload.pop("ftt_entry")
        connection.execute(
            sa.text("UPDATE guild_feature_settings SET payload = CAST(:payload AS jsonb) WHERE id = :id"),
            {"id": feature["id"], "payload": json.dumps(payload)},
        )

    connection.execute(
        sa.text("""
        DELETE FROM guild_access_policy_members AS member
        USING guild_access_policies AS policy, guild_config_revisions AS revision
        WHERE member.policy_id = policy.id AND policy.config_revision_id = revision.id
          AND revision.guild_id = :guild_id AND revision.status IN ('draft', 'published')
          AND policy.policy_key = 'role_request_allowed_roles'
          AND member.snowflake_id = :role_id
        """),
        parameters,
    )
    connection.execute(
        sa.text("""
        UPDATE guild_access_policies AS policy SET mode = 'disabled'
        FROM guild_config_revisions AS revision
        WHERE policy.config_revision_id = revision.id
          AND revision.guild_id = :guild_id AND revision.status IN ('draft', 'published')
          AND policy.policy_key = 'role_request_allowed_roles' AND policy.mode = 'restricted'
          AND NOT EXISTS (SELECT 1 FROM guild_access_policy_members WHERE policy_id = policy.id)
        """),
        {"guild_id": LSSD_GUILD_ID},
    )


def downgrade() -> None:
    """Do not automatically re-enable a withdrawn role or guess custom catalog positions."""

"""Add configurable shift selection to faction entry and role changes."""

import json
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260930_0086"
down_revision: str | None = "20260930_0085"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SHIFTS = [
    {"key": "am", "label": "AM Shift", "role_id": 1554676577609187338,
     "description": "Desde el reinicio del servidor hasta las 18:00 ARG.", "emoji": "🌤️"},
    {"key": "pm", "label": "PM Shift", "role_id": 1554676839899996301,
     "description": "Desde las 18:00 hasta las 00:00 ARG.", "emoji": "🌆"},
    {"key": "em", "label": "EM Shift", "role_id": 1554676886964142100,
     "description": "Desde las 00:00 ARG hasta el reinicio del servidor.", "emoji": "🌙"},
]


def upgrade() -> None:
    op.add_column("role_requests", sa.Column("shift_change", postgresql.JSONB))
    connection = op.get_bind()
    rows = connection.execute(sa.text("""
        SELECT feature.id, policy.id AS policy_id
        FROM guild_feature_settings feature
        JOIN guild_config_revisions revision ON revision.id = feature.config_revision_id
        JOIN guild_access_policies policy ON policy.config_revision_id = revision.id
        WHERE revision.guild_id = 1234674521416663070
          AND feature.feature_key = 'role_requests'
          AND NOT (feature.payload ? 'shifts')
          AND policy.policy_key = 'role_request_allowed_roles' AND policy.mode = 'restricted'
    """)).mappings().all()
    for row in rows:
        for shift in SHIFTS:
            connection.execute(sa.text("""
                INSERT INTO guild_access_policy_members (id, policy_id, snowflake_id, position)
                SELECT :id, :policy_id, :role_id, COALESCE(MAX(position), -1) + 1
                FROM guild_access_policy_members WHERE policy_id = :policy_id
                ON CONFLICT (policy_id, snowflake_id) DO NOTHING
            """), {"id": uuid.uuid4(), "policy_id": row["policy_id"], "role_id": shift["role_id"]})
        connection.execute(sa.text("""
            UPDATE guild_feature_settings SET payload = payload || CAST(:options AS jsonb)
            WHERE id = :id
        """), {"id": row["id"], "options": json.dumps({"shifts": SHIFTS})})


def downgrade() -> None:
    # Older config schemas forbid the new key; retain catalog roles and request operations.
    op.execute("UPDATE guild_feature_settings SET payload = payload - 'shifts' "
               "WHERE feature_key = 'role_requests'")
    op.drop_column("role_requests", "shift_change")

"""Enable configurable LSSD recipients after exhausted automatic review recovery."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261011_0091"
down_revision: str | None = "20261008_0090"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LSSD_GUILD_ID = 1234674521416663070
COMMAND_STAFF_ROLE_ID = 1234678029662294047
EXECUTIVE_STAFF_ROLE_ID = 1234678238182244414


def upgrade() -> None:
    op.get_bind().execute(
        sa.text(
            """
            INSERT INTO guild_feature_settings
                (id, config_revision_id, feature_key, enabled, schema_version, payload)
            SELECT gen_random_uuid(), revision.id, 'review_failure_notifications', true, 1,
                jsonb_build_object(
                    'role_ids', jsonb_build_array(CAST(:command AS bigint), CAST(:executive AS bigint)),
                    'user_ids', jsonb_build_array()
                )
            FROM guild_config_revisions AS revision
            WHERE revision.guild_id = :guild_id AND revision.status IN ('draft', 'published')
            ON CONFLICT (config_revision_id, feature_key) DO NOTHING
            """
        ),
        {"guild_id": LSSD_GUILD_ID, "command": COMMAND_STAFF_ROLE_ID, "executive": EXECUTIVE_STAFF_ROLE_ID},
    )


def downgrade() -> None:
    """Preserve recipient settings when rolling back the application."""

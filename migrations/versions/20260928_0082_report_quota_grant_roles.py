"""Authorize LSSD command and executive staff to grant report-owner rounds."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260928_0082"
down_revision: str | None = "20260927_0081"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LSSD_GUILD_ID = 1234674521416663070
COMMAND_ROLE_ID = 1234678029662294047
EXECUTIVE_ROLE_ID = 1234678238182244414


def upgrade() -> None:
    op.get_bind().execute(
        sa.text(
            """
            UPDATE guild_feature_settings AS feature
            SET payload = feature.payload || jsonb_build_object(
                'quota_grant_role_ids', jsonb_build_array(
                    CAST(:command_role_id AS bigint), CAST(:executive_role_id AS bigint)
                )
            )
            FROM guild_config_revisions AS revision
            WHERE revision.id = feature.config_revision_id
              AND revision.guild_id = :guild_id
              AND feature.feature_key = 'report_owner_access'
              AND NOT feature.payload ? 'quota_grant_role_ids'
            """
        ),
        {
            "guild_id": LSSD_GUILD_ID,
            "command_role_id": COMMAND_ROLE_ID,
            "executive_role_id": EXECUTIVE_ROLE_ID,
        },
    )


def downgrade() -> None:
    op.get_bind().execute(
        sa.text(
            """
            UPDATE guild_feature_settings AS feature
            SET payload = feature.payload - 'quota_grant_role_ids'
            FROM guild_config_revisions AS revision
            WHERE revision.id = feature.config_revision_id
              AND revision.guild_id = :guild_id
              AND feature.feature_key = 'report_owner_access'
            """
        ),
        {"guild_id": LSSD_GUILD_ID},
    )

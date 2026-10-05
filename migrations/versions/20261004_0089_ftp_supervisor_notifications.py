"""Configure additional FTP supervisor request mentions without granting permissions."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261004_0089"
down_revision: str | None = "20261001_0088"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LSSD_GUILD_ID = 1234674521416663070
FTP_MANAGEMENT_ROLE_ID = 1390426066455105598


def upgrade() -> None:
    op.get_bind().execute(
        sa.text(
            """
            UPDATE guild_feature_settings AS feature
            SET payload = feature.payload || jsonb_build_object(
                'supervisor_extra_notification_role_ids', jsonb_build_array(CAST(:role_id AS bigint))
            )
            FROM guild_config_revisions AS revision
            WHERE revision.id = feature.config_revision_id
              AND revision.guild_id = :guild_id
              AND feature.feature_key = 'ftp_requests'
              AND NOT feature.payload ? 'supervisor_extra_notification_role_ids'
            """
        ),
        {"guild_id": LSSD_GUILD_ID, "role_id": FTP_MANAGEMENT_ROLE_ID},
    )


def downgrade() -> None:
    op.get_bind().execute(
        sa.text(
            """
            UPDATE guild_feature_settings AS feature
            SET payload = feature.payload - 'supervisor_extra_notification_role_ids'
            FROM guild_config_revisions AS revision
            WHERE revision.id = feature.config_revision_id
              AND revision.guild_id = :guild_id
              AND feature.feature_key = 'ftp_requests'
            """
        ),
        {"guild_id": LSSD_GUILD_ID},
    )

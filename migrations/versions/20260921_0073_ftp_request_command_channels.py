"""Allow the FTP request command from additional configured channels."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260921_0073"
down_revision: str | None = "20260921_0072"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LSSD_GUILD_ID = 1234674521416663070
FEATURE_KEY = "ftp_requests"
FTP_CHAT_CHANNEL_ID = 1234749125820420139


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text(
            """
            UPDATE guild_feature_settings AS feature
            SET payload = feature.payload || jsonb_build_object(
                'additional_command_channel_ids', jsonb_build_array(CAST(:channel_id AS bigint))
            )
            FROM guild_config_revisions AS revision
            WHERE revision.id = feature.config_revision_id
              AND revision.guild_id = :guild_id
              AND feature.feature_key = :feature_key
            """
        ),
        {
            "guild_id": LSSD_GUILD_ID,
            "feature_key": FEATURE_KEY,
            "channel_id": FTP_CHAT_CHANNEL_ID,
        },
    )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text(
            """
            UPDATE guild_feature_settings AS feature
            SET payload = feature.payload - 'additional_command_channel_ids'
            FROM guild_config_revisions AS revision
            WHERE revision.id = feature.config_revision_id
              AND revision.guild_id = :guild_id
              AND feature.feature_key = :feature_key
            """
        ),
        {
            "guild_id": LSSD_GUILD_ID,
            "feature_key": FEATURE_KEY,
        },
    )

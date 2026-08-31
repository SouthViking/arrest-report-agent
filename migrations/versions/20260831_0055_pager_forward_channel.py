"""Forward LSSD pagers to the configured chat channel."""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260831_0055"
down_revision: str | None = "20260830_0054"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LSSD_GUILD_ID = 1234674521416663070
LSSD_CHAT_CHANNEL_ID = 1234675609536430152


def upgrade() -> None:
    op.get_bind().execute(
        sa.text(
            "UPDATE guild_feature_settings AS feature "
            "SET payload = feature.payload || CAST(:options AS jsonb) "
            "FROM guild_config_revisions AS revision "
            "WHERE feature.config_revision_id = revision.id "
            "AND feature.feature_key = 'pagers' "
            "AND revision.guild_id = :guild_id"
        ),
        {
            "guild_id": LSSD_GUILD_ID,
            "options": json.dumps({"forward_channel_id": LSSD_CHAT_CHANNEL_ID}),
        },
    )


def downgrade() -> None:
    op.get_bind().execute(
        sa.text(
            "UPDATE guild_feature_settings AS feature "
            "SET payload = feature.payload - 'forward_channel_id' "
            "FROM guild_config_revisions AS revision "
            "WHERE feature.config_revision_id = revision.id "
            "AND feature.feature_key = 'pagers' "
            "AND revision.guild_id = :guild_id"
        ),
        {"guild_id": LSSD_GUILD_ID},
    )

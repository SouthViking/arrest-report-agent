"""Update the LSSD TeamSpeak address in current and draft configurations."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261008_0090"
down_revision: str | None = "20261004_0089"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LSSD_GUILD_ID = 1234674521416663070
TEAMSPEAK_ADDRESS = "172.96.11.180:11189"


def upgrade() -> None:
    op.get_bind().execute(
        sa.text(
            """
            UPDATE guild_feature_settings AS feature
            SET payload = feature.payload || jsonb_build_object('address', CAST(:address AS text))
            FROM guild_config_revisions AS revision
            WHERE revision.id = feature.config_revision_id
              AND revision.guild_id = :guild_id
              AND revision.status IN ('draft', 'published')
              AND feature.feature_key = 'teamspeak'
            """
        ),
        {"guild_id": LSSD_GUILD_ID, "address": TEAMSPEAK_ADDRESS},
    )


def downgrade() -> None:
    """Keep the current server address when rolling application code back."""

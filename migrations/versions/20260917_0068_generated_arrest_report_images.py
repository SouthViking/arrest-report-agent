"""Enable deterministic arrest-report images for LSSD."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260917_0068"
down_revision: str | None = "20260917_0067"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LSSD_GUILD_ID = 1234674521416663070
FEATURE_KEY = "arrest_report_lookup"
OPTION_KEY = "report_attachment_mode"


def upgrade() -> None:
    """Select generated report images in every existing LSSD configuration revision."""

    op.get_bind().execute(
        sa.text(
            """
            UPDATE guild_feature_settings AS feature
            SET payload = feature.payload || jsonb_build_object(:option_key, 'generated')
            FROM guild_config_revisions AS revision
            WHERE revision.id = feature.config_revision_id
              AND revision.guild_id = :guild_id
              AND feature.feature_key = :feature_key
            """
        ),
        {
            "guild_id": LSSD_GUILD_ID,
            "feature_key": FEATURE_KEY,
            "option_key": OPTION_KEY,
        },
    )


def downgrade() -> None:
    """Return LSSD revisions to the backwards-compatible manual default."""

    op.get_bind().execute(
        sa.text(
            """
            UPDATE guild_feature_settings AS feature
            SET payload = feature.payload - :option_key
            FROM guild_config_revisions AS revision
            WHERE revision.id = feature.config_revision_id
              AND revision.guild_id = :guild_id
              AND feature.feature_key = :feature_key
            """
        ),
        {
            "guild_id": LSSD_GUILD_ID,
            "feature_key": FEATURE_KEY,
            "option_key": OPTION_KEY,
        },
    )

"""Authorize Command/Executive staff approval overrides for the LSSD guild."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261026_0095"
down_revision: str | None = "20261025_0094"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.get_bind().execute(
        sa.text("""
        UPDATE guild_feature_settings AS feature
        SET payload = jsonb_build_object('approval_override_role_ids',
            jsonb_build_array(CAST(:command AS bigint), CAST(:executive AS bigint))) || feature.payload
        FROM guild_config_revisions AS revision
        WHERE revision.id = feature.config_revision_id
          AND revision.guild_id = :guild
          AND revision.status IN ('draft', 'published')
          AND feature.feature_key = 'arrest_report_lookup'
    """),
        {"guild": 1234674521416663070, "command": 1234678029662294047, "executive": 1234678238182244414},
    )


def downgrade() -> None:
    op.get_bind().execute(
        sa.text("""
        UPDATE guild_feature_settings AS feature
        SET payload = payload - 'approval_override_role_ids'
        FROM guild_config_revisions AS revision
        WHERE revision.id = feature.config_revision_id
          AND revision.guild_id = 1234674521416663070
          AND revision.status IN ('draft', 'published')
          AND feature.feature_key = 'arrest_report_lookup'
    """)
    )

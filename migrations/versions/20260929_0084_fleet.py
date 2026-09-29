"""Enable deterministic fleet queries for LSSD with a configurable shared cache."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260929_0084"
down_revision: str | None = "20260928_0083"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.get_bind().execute(
        sa.text("""
        INSERT INTO guild_feature_settings
            (id, config_revision_id, feature_key, enabled, schema_version, payload)
        SELECT gen_random_uuid(), id, 'fleet', true, 1,
               jsonb_build_object('faction_id', 3, 'cache_seconds', 120)
        FROM guild_config_revisions
        WHERE guild_id = 1234674521416663070
        ON CONFLICT (config_revision_id, feature_key) DO NOTHING
    """)
    )


def downgrade() -> None:
    op.get_bind().execute(sa.text("DELETE FROM guild_feature_settings WHERE feature_key = 'fleet'"))

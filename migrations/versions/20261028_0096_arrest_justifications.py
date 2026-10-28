"""Persist per-report reminder pauses and seed configurable LSSD managers."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261028_0096"
down_revision: str | None = "20261026_0095"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "pending_arrest_justifications",
        sa.Column("interaction_id", sa.BigInteger(), primary_key=True),
        sa.Column("guild_id", sa.BigInteger(),
                  sa.ForeignKey("discord_guilds.discord_id", ondelete="CASCADE"), nullable=False),
        sa.Column("faction_id", sa.BigInteger(), nullable=False),
        sa.Column("report_id", sa.BigInteger(), nullable=False),
        sa.Column("officer_name", sa.String(100), nullable=False),
        sa.Column("reason", sa.String(500), nullable=False),
        sa.Column("authorized_by", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
        sa.Column("ended_by", sa.BigInteger()),
        sa.Column("end_interaction_id", sa.BigInteger()),
        sa.Column("end_reason", sa.String(20)),
        sa.CheckConstraint("end_reason IN ('replaced','removed','expired','completed')",
                           name="ck_arrest_justification_end_reason"),
    )
    op.create_index(
        "uq_active_arrest_justification", "pending_arrest_justifications",
        ["guild_id", "faction_id", "report_id"], unique=True,
        postgresql_where=sa.text("ended_at IS NULL"),
    )
    op.get_bind().execute(sa.text("""
        UPDATE guild_feature_settings AS feature
        SET payload = jsonb_build_object('justification_role_ids',
            jsonb_build_array(1234676131240869949::bigint,
                             1234678029662294047::bigint,
                             1234678238182244414::bigint)) || feature.payload
        FROM guild_config_revisions AS revision
        WHERE revision.id = feature.config_revision_id
          AND revision.guild_id = 1234674521416663070
          AND revision.status IN ('draft', 'published')
          AND feature.feature_key = 'pending_arrest_reports'
    """))


def downgrade() -> None:
    op.get_bind().execute(sa.text("""
        UPDATE guild_feature_settings AS feature
        SET payload = payload - 'justification_role_ids'
        FROM guild_config_revisions AS revision
        WHERE revision.id = feature.config_revision_id
          AND revision.guild_id = 1234674521416663070
          AND revision.status IN ('draft', 'published')
          AND feature.feature_key = 'pending_arrest_reports'
    """))
    op.drop_table("pending_arrest_justifications")

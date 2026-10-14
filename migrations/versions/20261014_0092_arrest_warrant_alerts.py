"""Notify Supervisory once about open warrants when a structured arrest is submitted."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261014_0092"
down_revision: str | None = "20261011_0091"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "arrest_warrant_alert_deliveries",
        sa.Column(
            "guild_id",
            sa.BigInteger(),
            sa.ForeignKey("discord_guilds.discord_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("sarp_arrest_id", sa.BigInteger(), primary_key=True),
        sa.Column("channel_id", sa.BigInteger(), primary_key=True),
        sa.Column("warrant_id", sa.BigInteger(), primary_key=True),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.get_bind().execute(
        sa.text("""
        INSERT INTO report_issue_types (code, display_name, description, position)
        SELECT 'open_arrest_warrant', 'Orden de arresto abierta',
               'Orden de arresto abierta que impide aprobar el reporte hasta verificar su cierre.',
               COALESCE(MAX(position), -1) + 1 FROM report_issue_types
        ON CONFLICT (code) DO NOTHING
    """)
    )
    op.get_bind().execute(
        sa.text("""
        UPDATE guild_feature_settings AS feature
        SET payload = jsonb_build_object(
            'warrant_alerts_enabled', true,
            'warrant_alert_channel_ids', COALESCE(
                NULLIF(pending.payload -> 'supervisor_alert_channel_ids', '[]'::jsonb),
                jsonb_build_array(CAST(:channel_id AS bigint))
            ),
            'warrant_alert_role_id', COALESCE(
                NULLIF(pending.payload -> 'supervisor_alert_role_id', 'null'::jsonb),
                to_jsonb(CAST(:role_id AS bigint))
            )
        ) || feature.payload
        FROM guild_config_revisions AS revision
        LEFT JOIN guild_feature_settings AS pending
          ON pending.config_revision_id = revision.id
         AND pending.feature_key = 'pending_arrest_reports'
        WHERE revision.id = feature.config_revision_id
          AND revision.guild_id = :guild_id
          AND revision.status IN ('draft', 'published')
          AND feature.feature_key = 'arrest_report_lookup'
    """),
        {"guild_id": 1234674521416663070, "channel_id": 1234678185124298853, "role_id": 1234676131240869949},
    )


def downgrade() -> None:
    op.get_bind().execute(
        sa.text("""
        UPDATE guild_feature_settings
        SET payload = payload - 'warrant_alerts_enabled' - 'warrant_alert_channel_ids'
                              - 'warrant_alert_role_id'
        WHERE feature_key = 'arrest_report_lookup'
    """)
    )
    # Keep the taxonomy row referenced by historical review findings.
    op.drop_table("arrest_warrant_alert_deliveries")

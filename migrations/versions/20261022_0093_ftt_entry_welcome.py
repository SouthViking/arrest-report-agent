"""Configure the updated FTT welcome without overwriting custom guild settings."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20261022_0093"
down_revision: str | None = "20261014_0092"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LSSD_GUILD_ID = 1234674521416663070
INFORMATION_CHANNEL_ID = 1234748926591107172
QUESTIONS_CHANNEL_ID = 1476796384102453268


def upgrade() -> None:
    op.get_bind().execute(
        sa.text("""
        UPDATE guild_feature_settings AS feature
        SET payload = jsonb_set(feature.payload, '{ftt_entry,welcome}',
            jsonb_build_object(
                'information_channel_id', CAST(:information_channel_id AS bigint),
                'questions_channel_id', CAST(:questions_channel_id AS bigint)
            ) || CASE
                WHEN feature.payload #> '{ftt_entry,welcome,reference_channel_ids}' =
                     '[1234675521560772628, 1467761647551774852]'::jsonb
                THEN jsonb_set(feature.payload #> '{ftt_entry,welcome}', '{reference_channel_ids}',
                     '[1467761647551774852, 1234675521560772628]'::jsonb)
                ELSE feature.payload #> '{ftt_entry,welcome}'
            END)
        FROM guild_config_revisions AS revision
        WHERE revision.id = feature.config_revision_id
          AND revision.guild_id = :guild_id
          AND revision.status IN ('draft', 'published')
          AND feature.feature_key = 'role_requests'
          AND jsonb_typeof(feature.payload #> '{ftt_entry,welcome}') = 'object'
        """),
        {
            "guild_id": LSSD_GUILD_ID,
            "information_channel_id": INFORMATION_CHANNEL_ID,
            "questions_channel_id": QUESTIONS_CHANNEL_ID,
        },
    )


def downgrade() -> None:
    # Remove only the seeded values; leave custom settings and archived revisions untouched.
    for key, channel_id in (
        ("information_channel_id", INFORMATION_CHANNEL_ID),
        ("questions_channel_id", QUESTIONS_CHANNEL_ID),
    ):
        op.get_bind().execute(
            sa.text("""
            UPDATE guild_feature_settings AS feature
            SET payload = jsonb_set(feature.payload, '{ftt_entry,welcome}',
                (feature.payload #> '{ftt_entry,welcome}') - CAST(:key AS text))
            FROM guild_config_revisions AS revision
            WHERE revision.id = feature.config_revision_id
              AND revision.guild_id = :guild_id
              AND revision.status IN ('draft', 'published')
              AND feature.feature_key = 'role_requests'
              AND feature.payload #> ARRAY['ftt_entry', 'welcome', CAST(:key AS text)] =
                  to_jsonb(CAST(:channel_id AS bigint))
            """),
            {"guild_id": LSSD_GUILD_ID, "key": key, "channel_id": channel_id},
        )

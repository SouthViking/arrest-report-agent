"""Add guided FTT entry role requests for LSSD."""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260921_0069"
down_revision: str | None = "20260917_0068"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LSSD_GUILD_ID = 1234674521416663070
FEATURE_KEY = "role_requests"
CATALOG_POLICY_KEY = "role_request_allowed_roles"

FIELD_TRAINING_TRAINEE_ROLE_ID = 1234675742013390849
DEPUTY_SHERIFF_ROLE_ID = 1234675779581771857
FIELD_STAFF_ROLE_ID = 1234675327872012328
CSD_TTCF_ROLE_ID = 1241871524424712314
CPD_GNT_ROLE_ID = 1258627732594495572
NPD_DLM_ROLE_ID = 1258630594741665893

ENTRY_ROLE_IDS = (
    FIELD_TRAINING_TRAINEE_ROLE_ID,
    DEPUTY_SHERIFF_ROLE_ID,
    FIELD_STAFF_ROLE_ID,
    CSD_TTCF_ROLE_ID,
    CPD_GNT_ROLE_ID,
    NPD_DLM_ROLE_ID,
)
ENTRY_OPTIONS = {
    "ftt_entry": {
        "base_role_ids": [
            FIELD_TRAINING_TRAINEE_ROLE_ID,
            DEPUTY_SHERIFF_ROLE_ID,
            FIELD_STAFF_ROLE_ID,
            CSD_TTCF_ROLE_ID,
        ],
        "stations": [
            {
                "key": "ganton",
                "label": "Ganton",
                "role_id": CPD_GNT_ROLE_ID,
                "emoji": "🏙️",
            },
            {
                "key": "dillimore",
                "label": "Dillimore",
                "role_id": NPD_DLM_ROLE_ID,
                "emoji": "🌲",
            },
        ],
    }
}


def upgrade() -> None:
    """Configure the guided package and allow its roles in the regular catalogue."""

    connection = op.get_bind()
    policy_ids = (
        connection.execute(
            sa.text(
                """
            SELECT policy.id
            FROM guild_access_policies AS policy
            JOIN guild_config_revisions AS revision
              ON revision.id = policy.config_revision_id
            WHERE revision.guild_id = :guild_id
              AND policy.policy_key = :policy_key
              AND policy.mode = 'restricted'
            """
            ),
            {"guild_id": LSSD_GUILD_ID, "policy_key": CATALOG_POLICY_KEY},
        )
        .scalars()
        .all()
    )
    for policy_id in policy_ids:
        for role_id in ENTRY_ROLE_IDS:
            connection.execute(
                sa.text(
                    "INSERT INTO guild_access_policy_members "
                    "(id, policy_id, snowflake_id, position) "
                    "SELECT :id, :policy_id, :role_id, COALESCE(MAX(position), -1) + 1 "
                    "FROM guild_access_policy_members WHERE policy_id = :policy_id "
                    "ON CONFLICT (policy_id, snowflake_id) DO NOTHING"
                ),
                {
                    "id": uuid.uuid4(),
                    "policy_id": policy_id,
                    "role_id": role_id,
                },
            )

    connection.execute(
        sa.text(
            """
            UPDATE guild_feature_settings AS feature
            SET payload = feature.payload || CAST(:options AS jsonb)
            FROM guild_config_revisions AS revision
            WHERE revision.id = feature.config_revision_id
              AND revision.guild_id = :guild_id
              AND feature.feature_key = :feature_key
              AND EXISTS (
                  SELECT 1
                  FROM guild_access_policies AS policy
                  WHERE policy.config_revision_id = revision.id
                    AND policy.policy_key = :policy_key
                    AND policy.mode = 'restricted'
              )
            """
        ),
        {
            "guild_id": LSSD_GUILD_ID,
            "feature_key": FEATURE_KEY,
            "policy_key": CATALOG_POLICY_KEY,
            "options": json.dumps(ENTRY_OPTIONS),
        },
    )

    op.drop_constraint("ck_role_request_kind", "role_requests", type_="check")
    op.create_check_constraint(
        "ck_role_request_kind",
        "role_requests",
        "kind IN ('normal','retirement','ftt_entry')",
    )


def downgrade() -> None:
    """Disable the guided flow while preserving its explicitly allowed catalogue roles."""

    connection = op.get_bind()
    connection.execute(
        sa.text(
            """
            UPDATE guild_feature_settings AS feature
            SET payload = feature.payload - CAST(:option_key AS text)
            FROM guild_config_revisions AS revision
            WHERE revision.id = feature.config_revision_id
              AND revision.guild_id = :guild_id
              AND feature.feature_key = :feature_key
            """
        ),
        {
            "guild_id": LSSD_GUILD_ID,
            "feature_key": FEATURE_KEY,
            "option_key": "ftt_entry",
        },
    )
    connection.execute(sa.text("UPDATE role_requests SET kind = 'normal' WHERE kind = 'ftt_entry'"))
    op.drop_constraint("ck_role_request_kind", "role_requests", type_="check")
    op.create_check_constraint(
        "ck_role_request_kind",
        "role_requests",
        "kind IN ('normal','retirement')",
    )

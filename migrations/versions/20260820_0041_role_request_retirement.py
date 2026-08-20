"""Enable the atomic Retired Employee request flow for LSSD."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260820_0041"
down_revision: str | None = "20260819_0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LSSD_GUILD_ID = 1234674521416663070
RETIRED_EMPLOYEE_ROLE_ID = 1255224113757294702
FEATURE_KEY = "role_requests"
CATALOG_POLICY_KEY = "role_request_allowed_roles"


def upgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text(
            "UPDATE guild_feature_settings feature "
            "SET payload = COALESCE(feature.payload, '{}'::jsonb) "
            "|| jsonb_build_object('retired_role_id', :retired_role_id) "
            "FROM guild_config_revisions revision "
            "WHERE feature.config_revision_id = revision.id "
            "AND feature.feature_key = :feature_key "
            "AND revision.guild_id = :guild_id "
            "AND EXISTS ("
            "SELECT 1 FROM guild_access_policies policy "
            "JOIN guild_access_policy_members member ON member.policy_id = policy.id "
            "WHERE policy.config_revision_id = revision.id "
            "AND policy.policy_key = :catalog_policy_key "
            "AND policy.mode = 'restricted' "
            "AND member.snowflake_id = :retired_role_id)"
        ),
        {
            "guild_id": LSSD_GUILD_ID,
            "retired_role_id": RETIRED_EMPLOYEE_ROLE_ID,
            "feature_key": FEATURE_KEY,
            "catalog_policy_key": CATALOG_POLICY_KEY,
        },
    )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text(
            "UPDATE guild_feature_settings feature "
            "SET payload = feature.payload - 'retired_role_id' "
            "FROM guild_config_revisions revision "
            "WHERE feature.config_revision_id = revision.id "
            "AND feature.feature_key = :feature_key "
            "AND revision.guild_id = :guild_id"
        ),
        {"guild_id": LSSD_GUILD_ID, "feature_key": FEATURE_KEY},
    )

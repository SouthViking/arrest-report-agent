"""Point the structured logbook policy at the actual LSSD logbook channel."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260819_0040"
down_revision: str | None = "20260819_0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LSSD_GUILD_ID = 1234674521416663070
PREVIOUS_CHANNEL_ID = 1261076248541532240
LOGBOOK_CHANNEL_ID = 1238385605264539709
POLICY_KEY = "logbook_channels"


def upgrade() -> None:
    _replace_channel(PREVIOUS_CHANNEL_ID, LOGBOOK_CHANNEL_ID)


def downgrade() -> None:
    _replace_channel(LOGBOOK_CHANNEL_ID, PREVIOUS_CHANNEL_ID)


def _replace_channel(source_channel_id: int, target_channel_id: int) -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text(
            "DELETE FROM guild_access_policy_members source "
            "USING guild_access_policies policy, guild_config_revisions revision "
            "WHERE source.policy_id = policy.id "
            "AND policy.config_revision_id = revision.id "
            "AND revision.guild_id = :guild_id "
            "AND policy.policy_key = :policy_key "
            "AND source.snowflake_id = :source_channel_id "
            "AND EXISTS ("
            "SELECT 1 FROM guild_access_policy_members target "
            "WHERE target.policy_id = source.policy_id "
            "AND target.snowflake_id = :target_channel_id)"
        ),
        {
            "guild_id": LSSD_GUILD_ID,
            "policy_key": POLICY_KEY,
            "source_channel_id": source_channel_id,
            "target_channel_id": target_channel_id,
        },
    )
    connection.execute(
        sa.text(
            "UPDATE guild_access_policy_members member "
            "SET snowflake_id = :target_channel_id "
            "FROM guild_access_policies policy, guild_config_revisions revision "
            "WHERE member.policy_id = policy.id "
            "AND policy.config_revision_id = revision.id "
            "AND revision.guild_id = :guild_id "
            "AND policy.policy_key = :policy_key "
            "AND member.snowflake_id = :source_channel_id"
        ),
        {
            "guild_id": LSSD_GUILD_ID,
            "policy_key": POLICY_KEY,
            "source_channel_id": source_channel_id,
            "target_channel_id": target_channel_id,
        },
    )

"""Add channel-scoped anonymous rumors with private author traceability."""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260927_0081"
down_revision: str | None = "20260927_0080"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LSSD_GUILD_ID = 1234674521416663070
GNT_CHANNEL_ID = 1234710666548477952
DLM_CHANNEL_ID = 1258640770504593501
CPD_GNT_ROLE_ID = 1258627732594495572
NPD_DLM_ROLE_ID = 1258630594741665893
FEATURE_KEY = "rumors"


def upgrade() -> None:
    op.create_table(
        "rumor_publications",
        sa.Column("interaction_id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "guild_id",
            sa.BigInteger(),
            sa.ForeignKey("discord_guilds.discord_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("role_id", sa.BigInteger(), nullable=False),
        sa.Column("author_user_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("guild_id", "channel_id", "message_id", name="uq_rumor_message"),
    )
    op.create_index(
        "ix_rumor_author", "rumor_publications", ["guild_id", "author_user_id", "created_at"]
    )
    connection = op.get_bind()
    for row in connection.execute(sa.text("SELECT id, guild_id FROM guild_config_revisions")).mappings():
        is_lssd = int(row["guild_id"]) == LSSD_GUILD_ID
        channels = (
            [
                {"channel_id": GNT_CHANNEL_ID, "role_id": CPD_GNT_ROLE_ID},
                {"channel_id": DLM_CHANNEL_ID, "role_id": NPD_DLM_ROLE_ID},
            ]
            if is_lssd
            else []
        )
        connection.execute(
            sa.text(
                "INSERT INTO guild_feature_settings "
                "(id, config_revision_id, feature_key, enabled, schema_version, payload) "
                "VALUES (:id, :revision_id, :key, :enabled, 1, CAST(:payload AS jsonb))"
            ),
            {
                "id": uuid.uuid4(),
                "revision_id": row["id"],
                "key": FEATURE_KEY,
                "enabled": is_lssd,
                "payload": json.dumps({"channels": channels}, separators=(",", ":")),
            },
        )


def downgrade() -> None:
    op.get_bind().execute(
        sa.text("DELETE FROM guild_feature_settings WHERE feature_key = :key"),
        {"key": FEATURE_KEY},
    )
    op.drop_index("ix_rumor_author", table_name="rumor_publications")
    op.drop_table("rumor_publications")

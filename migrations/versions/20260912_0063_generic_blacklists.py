"""Add generic channel-scoped blacklists and seed the CCW registry."""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260912_0063"
down_revision: str | None = "20260910_0062"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FEATURE_KEY = "blacklists"
LSSD_GUILD_ID = 1234674521416663070
CCW_CHANNEL_ID = 1530621801871441991
LSSD_MANAGER_ROLE_IDS = (1234678029662294047, 1234678238182244414)
CCW_PAYLOAD = {
    "manager_role_ids": list(LSSD_MANAGER_ROLE_IDS),
    "types": [
        {
            "key": "ccw",
            "display_name": "CCW",
            "reference_prefix": "CCW",
            "channel_ids": [CCW_CHANNEL_ID],
            "enabled": True,
        }
    ]
}


def upgrade() -> None:
    op.create_table(
        "blacklist_counters",
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("type_key", sa.String(length=40), nullable=False),
        sa.Column("last_number", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("last_number > 0", name="ck_blacklist_counter_positive"),
        sa.ForeignKeyConstraint(["guild_id"], ["discord_guilds.discord_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("guild_id", "type_key"),
    )
    op.create_table(
        "blacklist_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("type_key", sa.String(length=40), nullable=False),
        sa.Column("type_name", sa.String(length=50), nullable=False),
        sa.Column("reference_prefix", sa.String(length=12), nullable=False),
        sa.Column("number", sa.BigInteger(), nullable=False),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger()),
        sa.Column("interaction_id", sa.BigInteger(), nullable=False),
        sa.Column("author_user_id", sa.BigInteger(), nullable=False),
        sa.Column("pcu_profile_id", sa.BigInteger(), nullable=False),
        sa.Column("pcu_url", sa.String(length=200), nullable=False),
        sa.Column("character_name", sa.String(length=100), nullable=False),
        sa.Column("character_name_key", sa.String(length=100), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="publishing"),
        sa.Column("edited_by_user_id", sa.BigInteger()),
        sa.Column("edited_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_by_user_id", sa.BigInteger()),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("number > 0", name="ck_blacklist_number_positive"),
        sa.CheckConstraint("pcu_profile_id > 0", name="ck_blacklist_pcu_profile_positive"),
        sa.CheckConstraint(
            "status IN ('publishing','active','failed','revoked')",
            name="ck_blacklist_status",
        ),
        sa.ForeignKeyConstraint(["guild_id"], ["discord_guilds.discord_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("interaction_id"),
        sa.UniqueConstraint(
            "guild_id", "type_key", "number", name="uq_blacklist_type_number"
        ),
        sa.UniqueConstraint(
            "guild_id", "channel_id", "message_id", name="uq_blacklist_message"
        ),
    )
    op.create_index(
        "ix_blacklist_guild_pcu",
        "blacklist_entries",
        ["guild_id", "pcu_profile_id", "status"],
    )
    op.create_index(
        "ix_blacklist_guild_character",
        "blacklist_entries",
        ["guild_id", "character_name_key", "status"],
    )
    op.create_index(
        "ix_blacklist_guild_created",
        "blacklist_entries",
        ["guild_id", "created_at"],
    )
    _install_configuration(op.get_bind())


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text("DELETE FROM guild_feature_settings WHERE feature_key = :key"),
        {"key": FEATURE_KEY},
    )
    op.drop_index("ix_blacklist_guild_created", table_name="blacklist_entries")
    op.drop_index("ix_blacklist_guild_character", table_name="blacklist_entries")
    op.drop_index("ix_blacklist_guild_pcu", table_name="blacklist_entries")
    op.drop_table("blacklist_entries")
    op.drop_table("blacklist_counters")


def _install_configuration(connection: sa.Connection) -> None:
    revisions = connection.execute(
        sa.text("SELECT id, guild_id FROM guild_config_revisions")
    ).mappings()
    for config_revision in revisions:
        enabled = int(config_revision["guild_id"]) == LSSD_GUILD_ID and _has_operational_roles(
            connection,
            config_revision["id"],
        )
        payload = (
            CCW_PAYLOAD
            if int(config_revision["guild_id"]) == LSSD_GUILD_ID
            else {"types": [], "manager_role_ids": []}
        )
        connection.execute(
            sa.text(
                "INSERT INTO guild_feature_settings "
                "(id, config_revision_id, feature_key, enabled, schema_version, payload) "
                "VALUES (:id, :revision_id, :key, :enabled, 1, CAST(:payload AS jsonb))"
            ),
            {
                "id": uuid.uuid4(),
                "revision_id": config_revision["id"],
                "key": FEATURE_KEY,
                "enabled": enabled,
                "payload": json.dumps(payload, separators=(",", ":")),
            },
        )


def _has_operational_roles(connection: sa.Connection, revision_id: object) -> bool:
    return bool(
        connection.execute(
            sa.text(
                "SELECT EXISTS ("
                "SELECT 1 FROM guild_access_policies policy "
                "JOIN guild_access_policy_members member ON member.policy_id = policy.id "
                "WHERE policy.config_revision_id = :revision_id "
                "AND policy.policy_key = 'operational_staff_roles' "
                "AND policy.mode = 'restricted'"
                ")"
            ),
            {"revision_id": revision_id},
        ).scalar_one()
    )

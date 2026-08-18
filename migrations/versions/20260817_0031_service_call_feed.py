"""Add the configurable SARP service-call feed and delivery cursors."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260817_0031"
down_revision: str | None = "20260817_0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FEATURE_KEY = "service_calls"
POLICY_KEY = "service_call_channels"
LSSD_GUILD_ID = 1234674521416663070
LSSD_SERVICE_CALL_CHANNEL_ID = 1539037905014759485


def upgrade() -> None:
    op.create_table(
        "service_call_cursors",
        sa.Column(
            "guild_id",
            sa.BigInteger(),
            sa.ForeignKey("discord_guilds.discord_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("channel_id", sa.BigInteger(), primary_key=True),
        sa.Column("last_call_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "last_call_id > 0",
            name="ck_service_call_cursor_positive",
        ),
    )
    _install_configuration(op.get_bind())


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text("DELETE FROM guild_feature_settings WHERE feature_key = :key"),
        {"key": FEATURE_KEY},
    )
    connection.execute(
        sa.text("DELETE FROM guild_access_policies WHERE policy_key = :key"),
        {"key": POLICY_KEY},
    )
    op.drop_table("service_call_cursors")


def _install_configuration(connection: sa.Connection) -> None:
    revisions = (
        connection.execute(sa.text("SELECT id, guild_id FROM guild_config_revisions"))
        .mappings()
        .all()
    )
    for config_revision in revisions:
        revision_id = config_revision["id"]
        is_lssd = int(config_revision["guild_id"]) == LSSD_GUILD_ID
        policy_id = uuid.uuid4()
        connection.execute(
            sa.text(
                "INSERT INTO guild_access_policies "
                "(id, config_revision_id, policy_key, subject_type, mode) "
                "VALUES (:id, :revision_id, :key, 'channel', :mode)"
            ),
            {
                "id": policy_id,
                "revision_id": revision_id,
                "key": POLICY_KEY,
                "mode": "restricted" if is_lssd else "disabled",
            },
        )
        if is_lssd:
            connection.execute(
                sa.text(
                    "INSERT INTO guild_access_policy_members "
                    "(id, policy_id, snowflake_id, position) "
                    "VALUES (:id, :policy_id, :channel_id, 0)"
                ),
                {
                    "id": uuid.uuid4(),
                    "policy_id": policy_id,
                    "channel_id": LSSD_SERVICE_CALL_CHANNEL_ID,
                },
            )
        connection.execute(
            sa.text(
                "INSERT INTO guild_feature_settings "
                "(id, config_revision_id, feature_key, enabled, schema_version, payload) "
                "VALUES (:id, :revision_id, :key, :enabled, 1, '{}'::jsonb)"
            ),
            {
                "id": uuid.uuid4(),
                "revision_id": revision_id,
                "key": FEATURE_KEY,
                "enabled": is_lssd,
            },
        )

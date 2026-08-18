"""Add SARP arrest lookup configuration and report bindings."""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260818_0033"
down_revision: str | None = "20260817_0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FEATURE_KEY = "arrest_report_lookup"
LSSD_GUILD_ID = 1234674521416663070
LSSD_FACTION_ID = 3


def upgrade() -> None:
    op.create_table(
        "report_arrest_bindings",
        sa.Column(
            "guild_id",
            sa.BigInteger(),
            sa.ForeignKey("discord_guilds.discord_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("root_message_id", sa.BigInteger(), primary_key=True),
        sa.Column("parent_channel_id", sa.BigInteger(), nullable=False),
        sa.Column("thread_id", sa.BigInteger(), nullable=False),
        sa.Column("sarp_arrest_id", sa.BigInteger(), nullable=False),
        sa.Column("bound_by_user_id", sa.BigInteger(), nullable=False),
        sa.Column("trigger_message_id", sa.BigInteger(), nullable=False),
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
            "sarp_arrest_id > 0",
            name="ck_report_arrest_binding_positive",
        ),
    )
    op.create_index(
        "ix_report_arrest_binding_arrest",
        "report_arrest_bindings",
        ["guild_id", "sarp_arrest_id"],
    )
    _install_configuration(op.get_bind())


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text("DELETE FROM guild_feature_settings WHERE feature_key = :key"),
        {"key": FEATURE_KEY},
    )
    op.drop_index(
        "ix_report_arrest_binding_arrest",
        table_name="report_arrest_bindings",
    )
    op.drop_table("report_arrest_bindings")


def _install_configuration(connection: sa.Connection) -> None:
    revisions = (
        connection.execute(sa.text("SELECT id, guild_id FROM guild_config_revisions"))
        .mappings()
        .all()
    )
    for config_revision in revisions:
        is_lssd = int(config_revision["guild_id"]) == LSSD_GUILD_ID
        payload = {"faction_id": LSSD_FACTION_ID} if is_lssd else {}
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
                "enabled": is_lssd,
                "payload": json.dumps(payload),
            },
        )

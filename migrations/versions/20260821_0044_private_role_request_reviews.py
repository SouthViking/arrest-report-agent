"""Persist role requests and route sensitive reviews to private channels."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260821_0044"
down_revision: str | None = "20260820_0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LSSD_GUILD_ID = 1234674521416663070
POLICY_KEY = "role_request_review_channels"
LSSD_REVIEW_CHANNEL_IDS = (1261076248541532240, 1234678185124298853)


def upgrade() -> None:
    op.create_table(
        "role_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "guild_id",
            sa.BigInteger(),
            sa.ForeignKey("discord_guilds.discord_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("interaction_id", sa.BigInteger(), nullable=False),
        sa.Column("requester_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("add_role_ids", postgresql.ARRAY(sa.BigInteger()), nullable=False),
        sa.Column("remove_role_ids", postgresql.ARRAY(sa.BigInteger()), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("public_channel_id", sa.BigInteger(), nullable=False),
        sa.Column("public_message_id", sa.BigInteger()),
        sa.Column("reviewer_id", sa.BigInteger()),
        sa.Column("decision_comment", sa.Text()),
        sa.Column(
            "approved_add_role_ids",
            postgresql.ARRAY(sa.BigInteger()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "approved_remove_role_ids",
            postgresql.ARRAY(sa.BigInteger()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "unapproved_add_role_ids",
            postgresql.ARRAY(sa.BigInteger()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "unapproved_remove_role_ids",
            postgresql.ARRAY(sa.BigInteger()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("requester_dm_delivered", sa.Boolean()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("kind IN ('normal','retirement')", name="ck_role_request_kind"),
        sa.CheckConstraint(
            "status IN ('publishing','pending','processing','approved',"
            "'partially_approved','rejected','failed')",
            name="ck_role_request_status",
        ),
        sa.CheckConstraint("version > 0", name="ck_role_request_version_positive"),
        sa.UniqueConstraint("interaction_id", name="uq_role_requests_interaction_id"),
        sa.UniqueConstraint(
            "guild_id",
            "public_channel_id",
            "public_message_id",
            name="uq_role_request_public_message",
        ),
    )
    op.create_index(
        "ix_role_request_requester", "role_requests", ["guild_id", "requester_id", "created_at"]
    )
    op.create_index("ix_role_request_status", "role_requests", ["guild_id", "status", "created_at"])
    op.create_index(
        "uq_role_request_live_requester",
        "role_requests",
        ["guild_id", "requester_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('publishing','pending','processing')"),
    )
    op.create_table(
        "role_request_review_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "request_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("role_requests.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("request_id", "channel_id", name="uq_role_request_review_channel"),
        sa.UniqueConstraint("channel_id", "message_id", name="uq_role_request_review_message"),
    )
    op.create_index(
        "ix_role_request_review_request", "role_request_review_messages", ["request_id"]
    )

    connection = op.get_bind()
    revisions = connection.execute(sa.text("SELECT id, guild_id FROM guild_config_revisions")).mappings()
    for config_revision in revisions:
        policy_id = uuid.uuid4()
        enabled = int(config_revision["guild_id"]) == LSSD_GUILD_ID
        connection.execute(
            sa.text(
                "INSERT INTO guild_access_policies "
                "(id, config_revision_id, policy_key, subject_type, mode) "
                "VALUES (:id, :revision_id, :key, 'channel', :mode)"
            ),
            {
                "id": policy_id,
                "revision_id": config_revision["id"],
                "key": POLICY_KEY,
                "mode": "restricted" if enabled else "disabled",
            },
        )
        if enabled:
            for position, channel_id in enumerate(LSSD_REVIEW_CHANNEL_IDS):
                connection.execute(
                    sa.text(
                        "INSERT INTO guild_access_policy_members "
                        "(id, policy_id, snowflake_id, position) "
                        "VALUES (:id, :policy_id, :channel_id, :position)"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "policy_id": policy_id,
                        "channel_id": channel_id,
                        "position": position,
                    },
                )


def downgrade() -> None:
    op.get_bind().execute(
        sa.text("DELETE FROM guild_access_policies WHERE policy_key = :key"),
        {"key": POLICY_KEY},
    )
    op.drop_index("ix_role_request_review_request", table_name="role_request_review_messages")
    op.drop_table("role_request_review_messages")
    op.drop_index("uq_role_request_live_requester", table_name="role_requests")
    op.drop_index("ix_role_request_status", table_name="role_requests")
    op.drop_index("ix_role_request_requester", table_name="role_requests")
    op.drop_table("role_requests")

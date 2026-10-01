"""Persistent instructor offers, individual slots, and assigned FTP requests."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20261001_0088"
down_revision: str | None = "20261001_0087"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ftp_availability_offers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "guild_id",
            sa.BigInteger,
            sa.ForeignKey("discord_guilds.discord_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("channel_id", sa.BigInteger, nullable=False),
        sa.Column("message_id", sa.BigInteger),
        sa.Column("interaction_id", sa.BigInteger, nullable=False, unique=True),
        sa.Column("instructor_id", sa.BigInteger, nullable=False),
        sa.Column("comments", sa.String(500), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revision", sa.Integer, nullable=False, server_default="0"),
        sa.Column("sync_due_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("status IN ('publishing','open','closed','failed')", name="ck_ftp_offer_status"),
        sa.UniqueConstraint("guild_id", "channel_id", "message_id", name="uq_ftp_offer_message"),
    )
    op.create_index("ix_ftp_offer_instructor", "ftp_availability_offers", ["guild_id", "instructor_id"])
    op.create_index("ix_ftp_offer_sync", "ftp_availability_offers", ["guild_id", "sync_due_at"])
    op.create_table(
        "ftp_availability_slots",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "offer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ftp_availability_offers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("position", sa.Integer, nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("trainee_id", sa.BigInteger),
        sa.Column("phase", sa.String(20)),
        sa.Column("comments", sa.String(500), nullable=False, server_default=""),
        sa.Column(
            "request_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("ftp_training_requests.id", deferrable=True, initially="DEFERRED"),
            unique=True,
        ),
        sa.Column("notification_due", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.CheckConstraint("status IN ('free','reserved','closed','expired')", name="ck_ftp_slot_status"),
        sa.CheckConstraint("ends_at > starts_at", name="ck_ftp_slot_times"),
        sa.CheckConstraint("position BETWEEN 1 AND 5", name="ck_ftp_slot_position"),
        sa.CheckConstraint(
            "(status = 'reserved' AND trainee_id IS NOT NULL AND request_id IS NOT NULL "
            "AND phase IS NOT NULL AND phase IN ('phase_1','phase_2','phase_3')) OR "
            "(status <> 'reserved' AND trainee_id IS NULL AND request_id IS NULL AND phase IS NULL "
            "AND NOT notification_due)",
            name="ck_ftp_slot_booking",
        ),
        sa.UniqueConstraint("offer_id", "position", name="uq_ftp_slot_position"),
    )
    op.create_index(
        "ix_ftp_slot_expiry",
        "ftp_availability_slots",
        ["ends_at"],
        postgresql_where=sa.text("status = 'free'"),
    )


def downgrade() -> None:
    op.drop_table("ftp_availability_slots")
    op.drop_table("ftp_availability_offers")

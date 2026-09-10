"""Add provider-neutral contribution ingestion and normalization storage."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260910_0062"
down_revision: str | None = "20260902_0061"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "supporter_profiles",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("discord_user_id", sa.BigInteger(), nullable=False, unique=True),
        sa.Column("display_name", sa.String(length=200)),
        sa.Column("mention_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("discord_user_id > 0", name="ck_supporter_profile_discord_user"),
    )
    op.create_table(
        "supporter_provider_links",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "profile_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("supporter_profiles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("provider_supporter_id", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "provider", "provider_supporter_id", name="uq_supporter_provider_identity"
        ),
    )
    op.create_index(
        "ix_supporter_provider_link_profile", "supporter_provider_links", ["profile_id"]
    )
    op.create_table(
        "support_exchange_rates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("source", sa.String(length=50), nullable=False),
        sa.Column("requested_date", sa.Date(), nullable=False),
        sa.Column("effective_date", sa.Date(), nullable=False),
        sa.Column("base_currency", sa.String(length=3), nullable=False),
        sa.Column("quote_currency", sa.String(length=3), nullable=False),
        sa.Column("rate", sa.Numeric(precision=24, scale=12), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("rate > 0", name="ck_support_exchange_rate_positive"),
        sa.CheckConstraint(
            "base_currency <> quote_currency",
            name="ck_support_exchange_rate_distinct_currencies",
        ),
        sa.UniqueConstraint(
            "source",
            "requested_date",
            "base_currency",
            "quote_currency",
            name="uq_support_exchange_rate_lookup",
        ),
    )
    op.create_table(
        "support_contributions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("provider_contribution_id", sa.String(length=100), nullable=False),
        sa.Column("transaction_id", sa.String(length=255)),
        sa.Column("provider_supporter_id", sa.String(length=100)),
        sa.Column(
            "supporter_profile_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("supporter_profiles.id", ondelete="SET NULL"),
        ),
        sa.Column("supporter_name", sa.String(length=200)),
        sa.Column("supporter_name_type", sa.String(length=50)),
        sa.Column("original_amount", sa.Numeric(precision=18, scale=6), nullable=False),
        sa.Column("original_currency", sa.String(length=3), nullable=False),
        sa.Column("normalized_amount_usd", sa.Numeric(precision=18, scale=6)),
        sa.Column("exchange_rate_to_usd", sa.Numeric(precision=24, scale=12)),
        sa.Column("exchange_rate_source", sa.String(length=50)),
        sa.Column("exchange_rate_date", sa.Date()),
        sa.Column(
            "normalization_status",
            sa.String(length=20),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("normalization_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "normalization_next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("normalization_last_error", sa.String(length=500)),
        sa.Column("normalized_at", sa.DateTime(timezone=True)),
        sa.Column("coffee_count", sa.Integer()),
        sa.Column("coffee_price", sa.Numeric(precision=18, scale=6)),
        sa.Column("total_amount_charged", sa.Numeric(precision=18, scale=6)),
        sa.Column("application_fee", sa.Numeric(precision=18, scale=6)),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("contributed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("refunded_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "original_amount >= 0", name="ck_support_contribution_original_amount"
        ),
        sa.CheckConstraint(
            "normalized_amount_usd IS NULL OR normalized_amount_usd >= 0",
            name="ck_support_contribution_normalized_amount",
        ),
        sa.CheckConstraint(
            "exchange_rate_to_usd IS NULL OR exchange_rate_to_usd > 0",
            name="ck_support_contribution_exchange_rate",
        ),
        sa.CheckConstraint(
            "coffee_count IS NULL OR coffee_count >= 0",
            name="ck_support_contribution_coffee_count",
        ),
        sa.CheckConstraint(
            "status IN ('succeeded','refunded')", name="ck_support_contribution_status"
        ),
        sa.CheckConstraint(
            "normalization_status IN ('pending','completed','retry','failed')",
            name="ck_support_contribution_normalization_status",
        ),
        sa.CheckConstraint(
            "normalization_attempts >= 0",
            name="ck_support_contribution_normalization_attempts",
        ),
        sa.CheckConstraint(
            "normalization_status <> 'completed' OR "
            "(normalized_amount_usd IS NOT NULL AND exchange_rate_to_usd IS NOT NULL "
            "AND exchange_rate_source IS NOT NULL AND exchange_rate_date IS NOT NULL "
            "AND normalized_at IS NOT NULL)",
            name="ck_support_contribution_completed_normalization",
        ),
        sa.UniqueConstraint(
            "provider",
            "provider_contribution_id",
            name="uq_support_contribution_provider_id",
        ),
    )
    op.create_index(
        "ix_support_contribution_status_date",
        "support_contributions",
        ["status", "contributed_at"],
    )
    op.create_index(
        "ix_support_contribution_supporter",
        "support_contributions",
        ["provider", "provider_supporter_id"],
    )
    op.create_index(
        "ix_support_contribution_profile", "support_contributions", ["supporter_profile_id"]
    )
    op.create_index(
        "ix_support_contribution_normalization_due",
        "support_contributions",
        ["normalization_status", "normalization_next_attempt_at"],
    )
    op.create_table(
        "support_webhook_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("provider_event_key", sa.String(length=64), nullable=False, unique=True),
        sa.Column("provider", sa.String(length=50), nullable=False),
        sa.Column("provider_event_id", sa.String(length=100), nullable=False),
        sa.Column("event_type", sa.String(length=30), nullable=False),
        sa.Column("live_mode", sa.Boolean(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("provider_contribution_id", sa.String(length=100), nullable=False),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False),
        sa.Column("event_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("ignored_reason", sa.String(length=100)),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("event_type IN ('created','refunded')", name="ck_support_event_type"),
        sa.CheckConstraint("attempt > 0", name="ck_support_event_attempt"),
        sa.CheckConstraint(
            "status IN ('processed','ignored')", name="ck_support_event_status"
        ),
    )
    op.create_index(
        "ix_support_webhook_event_received",
        "support_webhook_events",
        ["provider", "received_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_support_webhook_event_received", table_name="support_webhook_events")
    op.drop_table("support_webhook_events")
    op.drop_index(
        "ix_support_contribution_normalization_due", table_name="support_contributions"
    )
    op.drop_index("ix_support_contribution_profile", table_name="support_contributions")
    op.drop_index("ix_support_contribution_supporter", table_name="support_contributions")
    op.drop_index("ix_support_contribution_status_date", table_name="support_contributions")
    op.drop_table("support_contributions")
    op.drop_table("support_exchange_rates")
    op.drop_index("ix_supporter_provider_link_profile", table_name="supporter_provider_links")
    op.drop_table("supporter_provider_links")
    op.drop_table("supporter_profiles")

"""Allow structured submissions without a known SARP report identifier."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260901_0058"
down_revision: str | None = "20260901_0057"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_arrest_submission_report_positive",
        "arrest_report_submissions",
        type_="check",
    )
    op.alter_column(
        "arrest_report_submissions",
        "sarp_arrest_id",
        existing_type=sa.BigInteger(),
        nullable=True,
    )
    op.create_check_constraint(
        "ck_arrest_submission_report_positive",
        "arrest_report_submissions",
        "sarp_arrest_id IS NULL OR sarp_arrest_id > 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_arrest_submission_report_positive",
        "arrest_report_submissions",
        type_="check",
    )
    op.alter_column(
        "arrest_report_submissions",
        "sarp_arrest_id",
        existing_type=sa.BigInteger(),
        nullable=False,
    )
    op.create_check_constraint(
        "ck_arrest_submission_report_positive",
        "arrest_report_submissions",
        "sarp_arrest_id > 0",
    )

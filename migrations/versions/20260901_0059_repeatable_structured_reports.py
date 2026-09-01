"""Allow more than one structured submission for the same SARP report."""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260901_0059"
down_revision: str | None = "20260901_0058"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "uq_arrest_submission_guild_report",
        "arrest_report_submissions",
        type_="unique",
    )
    op.create_index(
        "ix_arrest_submission_guild_report",
        "arrest_report_submissions",
        ["guild_id", "sarp_arrest_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_arrest_submission_guild_report",
        table_name="arrest_report_submissions",
    )
    op.create_unique_constraint(
        "uq_arrest_submission_guild_report",
        "arrest_report_submissions",
        ["guild_id", "sarp_arrest_id"],
    )

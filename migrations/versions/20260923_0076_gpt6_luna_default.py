"""Move active Terra agent configurations to GPT-6 Luna."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260923_0076"
down_revision: str | None = "20260921_0075"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PREVIOUS_MODEL = "gpt-5.6-terra"
NEW_MODEL = "gpt-6-luna"


def upgrade() -> None:
    _replace_model(PREVIOUS_MODEL, NEW_MODEL)


def downgrade() -> None:
    _replace_model(NEW_MODEL, PREVIOUS_MODEL)


def _replace_model(current: str, replacement: str) -> None:
    # Keep archived revisions as historical snapshots and preserve any custom model.
    op.get_bind().execute(
        sa.text(
            """
            UPDATE guild_agent_settings AS agent
            SET model = :replacement
            FROM guild_config_revisions AS revision
            WHERE revision.id = agent.config_revision_id
              AND revision.status IN ('draft', 'published')
              AND agent.model = :current
            """
        ),
        {"current": current, "replacement": replacement},
    )

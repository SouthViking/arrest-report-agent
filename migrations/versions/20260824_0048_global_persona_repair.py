"""Repair the global persona pointer after the member-analysis migration."""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260824_0048"
down_revision: str | None = "20260824_0047"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERSONA_REVISIONS = (
    ("walter_vigil", 51, 52),
    ("roy_velazco", 58, 59),
)


def upgrade() -> None:
    connection = op.get_bind()
    for slug, previous_revision, next_revision in PERSONA_REVISIONS:
        connection.execute(
            sa.text(
                "UPDATE bot_application_profile profile "
                "SET persona_revision_id = next_revision.id "
                "FROM personas persona "
                "JOIN persona_revisions previous_revision "
                "ON previous_revision.persona_id = persona.id "
                "AND previous_revision.revision = :previous_revision "
                "JOIN persona_revisions next_revision "
                "ON next_revision.persona_id = persona.id "
                "AND next_revision.revision = :next_revision "
                "WHERE persona.slug = :slug "
                "AND profile.persona_revision_id = previous_revision.id"
            ),
            {
                "slug": slug,
                "previous_revision": previous_revision,
                "next_revision": next_revision,
            },
        )


def downgrade() -> None:
    # Revision 0047 owns the persona transition. Keeping this repair in place
    # leaves the database valid while 0047 remains applied; its downgrade
    # restores the previous global pointer together with guild assignments.
    pass

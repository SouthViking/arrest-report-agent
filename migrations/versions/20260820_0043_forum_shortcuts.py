"""Add the public guild-configured forum shortcut directory."""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260820_0043"
down_revision: str | None = "20260820_0042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FEATURE_KEY = "forum_shortcuts"
LSSD_GUILD_ID = 1234674521416663070
LSSD_OPTIONS = {
    "organization_name": "Los Santos Sheriff's Department",
    "description": "Consulta rápidamente nuestros espacios y procedimientos oficiales.",
    "banner_url": "https://i.imgur.com/gBxZZIT.gif",
    "sections": [
        {
            "title": "🏛️ Foro institucional",
            "links": [
                {
                    "label": "Página principal",
                    "url": "https://sheriff.sarp.es/index.php",
                }
            ],
        },
        {
            "title": "👥 Ingreso y desarrollo",
            "links": [
                {
                    "label": "Oportunidades de empleo",
                    "url": "https://sheriff.sarp.es/viewforum.php?f=3",
                },
                {
                    "label": "Transferencias laterales",
                    "url": "https://sheriff.sarp.es/viewtopic.php?t=162",
                },
                {
                    "label": "Reincorporaciones",
                    "url": "https://sheriff.sarp.es/viewtopic.php?t=104",
                },
                {
                    "label": "Field Training Program",
                    "url": "https://sheriff.sarp.es/viewforum.php?f=10",
                },
            ],
        },
        {
            "title": "📋 Solicitudes y participación pública",
            "links": [
                {
                    "label": "Quejas y elogios públicos",
                    "url": "https://sheriff.sarp.es/viewtopic.php?t=497",
                },
                {
                    "label": "Licencias CCW y de caza",
                    "url": "https://sheriff.sarp.es/viewforum.php?f=135",
                },
            ],
        },
    ],
}


def upgrade() -> None:
    connection = op.get_bind()
    revisions = (
        connection.execute(sa.text("SELECT id, guild_id FROM guild_config_revisions")).mappings().all()
    )
    for config_revision in revisions:
        enabled = int(config_revision["guild_id"]) == LSSD_GUILD_ID
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
                "payload": json.dumps(LSSD_OPTIONS if enabled else {}),
            },
        )


def downgrade() -> None:
    op.get_bind().execute(
        sa.text("DELETE FROM guild_feature_settings WHERE feature_key = :key"),
        {"key": FEATURE_KEY},
    )

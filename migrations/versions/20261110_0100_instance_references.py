"""Persist legacy public references without replacing explicit configuration."""

import json
import uuid
from pathlib import Path

import sqlalchemy as sa
from alembic import op

revision = "20261110_0100"
down_revision = "20261031_0099"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Frozen migration input: future edits to the fresh-install preset must not change history.
    source = Path(__file__).parents[1] / "data" / "20261110_0100_instance_references.json"
    legacy = json.loads(source.read_text(encoding="utf-8"))
    connection = op.get_bind()
    for guild_id, options in legacy.items():
        revisions = (
            connection.execute(
                sa.text(
                    "SELECT id FROM guild_config_revisions "
                    "WHERE guild_id = :guild_id AND status IN ('draft', 'published')"
                ),
                {"guild_id": int(guild_id)},
            )
            .scalars()
            .all()
        )
        for config_id in revisions:
            for key, feature in options["features"].items():
                connection.execute(
                    sa.text("""
                    INSERT INTO guild_feature_settings
                        (id, config_revision_id, feature_key, enabled, schema_version, payload)
                    VALUES (:id, :config_id, :key, :enabled, 1, CAST(:payload AS jsonb))
                    ON CONFLICT (config_revision_id, feature_key) DO NOTHING
                """),
                    {
                        "id": uuid.uuid4(),
                        "config_id": config_id,
                        "key": key,
                        "enabled": feature["enabled"],
                        "payload": json.dumps(feature["payload"]),
                    },
                )


def downgrade() -> None:
    """Retain operator choices and contribution history when rolling code back."""

"""Allow an optional unit number after the callsign letter."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260813_0015"
down_revision: str | None = "20260813_0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERSONA_REVISIONS = (
    ("walter_vigil", 29, 30),
    ("roy_velazco", 36, 37),
)
PERSONA_SCHEMA_VERSION = 26
CALLSIGN_MESSAGES = {
    "walter_vigil": (
        "Utilice dos o tres números, una letra de A a Z y, opcionalmente, un número final. "
        "Ejemplos: **280L**, **64A** o **60F1**."
    ),
    "roy_velazco": (
        "Ese callsign no sirve. Pon dos o tres números, una letra de la A a la Z y, "
        "si corresponde, un número al final; por ejemplo **280L**, **64A** o **60F1**."
    ),
}


def upgrade() -> None:
    _install_personas(op.get_bind())


def downgrade() -> None:
    _downgrade_personas(op.get_bind())


def _install_personas(connection: sa.Connection) -> None:
    for slug, previous_revision, next_revision in PERSONA_REVISIONS:
        source = (
            connection.execute(
                sa.text(
                    "SELECT p.id AS persona_id, pr.id AS revision_id, pr.payload "
                    "FROM personas p JOIN persona_revisions pr ON pr.persona_id = p.id "
                    "WHERE p.slug = :slug AND pr.revision = :revision"
                ),
                {"slug": slug, "revision": previous_revision},
            )
            .mappings()
            .one_or_none()
        )
        if source is None:
            continue
        existing = connection.execute(
            sa.text(
                "SELECT id FROM persona_revisions "
                "WHERE persona_id = :persona_id AND revision = :revision"
            ),
            {"persona_id": source["persona_id"], "revision": next_revision},
        ).scalar_one_or_none()
        revision_id = existing or uuid.uuid4()
        if existing is None:
            payload = dict(source["payload"])
            payload["revision"] = next_revision
            payload["schema_version"] = PERSONA_SCHEMA_VERSION
            payload["messages"]["interface"]["callsigns"]["invalid_format"] = (
                CALLSIGN_MESSAGES[slug]
            )
            serialized = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            connection.execute(
                sa.text(
                    "INSERT INTO persona_revisions "
                    "(id, persona_id, revision, schema_version, status, payload, checksum, published_at) "
                    "VALUES (:id, :persona_id, :revision, :schema_version, 'published', "
                    "CAST(:payload AS jsonb), :checksum, now())"
                ),
                {
                    "id": revision_id,
                    "persona_id": source["persona_id"],
                    "revision": next_revision,
                    "schema_version": PERSONA_SCHEMA_VERSION,
                    "payload": serialized.decode(),
                    "checksum": hashlib.sha256(serialized).hexdigest(),
                },
            )
            connection.execute(
                sa.text(
                    "INSERT INTO persona_assets "
                    "(id, persona_revision_id, kind, object_key, sha256, content_type, size_bytes) "
                    "SELECT :id, :next_revision, kind, object_key, sha256, content_type, size_bytes "
                    "FROM persona_assets WHERE persona_revision_id = :previous_revision"
                ),
                {
                    "id": uuid.uuid4(),
                    "next_revision": revision_id,
                    "previous_revision": source["revision_id"],
                },
            )
        connection.execute(
            sa.text(
                "UPDATE guild_persona_assignments SET persona_revision_id = :next_revision "
                "WHERE persona_revision_id = :previous_revision"
            ),
            {"next_revision": revision_id, "previous_revision": source["revision_id"]},
        )
        connection.execute(
            sa.text(
                "UPDATE bot_application_profile SET persona_revision_id = :next_revision "
                "WHERE persona_revision_id = :previous_revision"
            ),
            {"next_revision": revision_id, "previous_revision": source["revision_id"]},
        )


def _downgrade_personas(connection: sa.Connection) -> None:
    for slug, previous_revision, next_revision in PERSONA_REVISIONS:
        rows = (
            connection.execute(
                sa.text(
                    "SELECT pr.revision, pr.id FROM personas p "
                    "JOIN persona_revisions pr ON pr.persona_id = p.id "
                    "WHERE p.slug = :slug AND pr.revision IN (:previous, :next)"
                ),
                {"slug": slug, "previous": previous_revision, "next": next_revision},
            )
            .mappings()
            .all()
        )
        ids = {int(row["revision"]): row["id"] for row in rows}
        previous_id, next_id = ids.get(previous_revision), ids.get(next_revision)
        if previous_id is None or next_id is None:
            continue
        connection.execute(
            sa.text(
                "UPDATE guild_persona_assignments SET persona_revision_id = :previous "
                "WHERE persona_revision_id = :next"
            ),
            {"previous": previous_id, "next": next_id},
        )
        connection.execute(
            sa.text(
                "UPDATE bot_application_profile SET persona_revision_id = :previous "
                "WHERE persona_revision_id = :next"
            ),
            {"previous": previous_id, "next": next_id},
        )
        connection.execute(
            sa.text("DELETE FROM persona_revisions WHERE id = :next"),
            {"next": next_id},
        )

"""Clarify pager usage and keep the callsign directory operationally neutral."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260812_0008"
down_revision: str | None = "20260812_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERSONA_REVISIONS = (
    ("walter_vigil", 22, 23),
    ("roy_velazco", 29, 30),
)
PERSONA_SCHEMA_VERSION = 21
INVALID_FORMAT_MESSAGES = {
    "walter_vigil": (
        "Utilice entre dos y tres números seguidos de L, F, B o S. "
        "Ejemplos: **280L**, **60F**, **282B** o **281S**."
    ),
    "roy_velazco": (
        "Ese callsign no sirve. Pon dos o tres números y termina en L, F, B o S; "
        "por ejemplo **280L**, **60F**, **282B** o **281S**."
    ),
}


def upgrade() -> None:
    connection = op.get_bind()
    for slug, previous_revision, next_revision in PERSONA_REVISIONS:
        source = (
            connection.execute(
                sa.text(
                    "SELECT p.id AS persona_id, pr.id AS revision_id, pr.schema_version, pr.payload "
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
        if existing is None:
            payload = dict(source["payload"])
            payload["revision"] = next_revision
            payload["schema_version"] = PERSONA_SCHEMA_VERSION
            callsigns = payload["messages"]["interface"]["callsigns"]
            callsigns["invalid_format"] = INVALID_FORMAT_MESSAGES[slug]
            callsigns["directory_intro"] = "Personal con un callsign registrado en este servidor."
            if slug == "roy_velazco":
                callsigns["directory_empty"] = (
                    "No hay callsigns registrados para miembros activos del servidor."
                )
                callsigns["navigation_owner_only"] = (
                    "Solo quien abrió el directorio puede cambiar de página."
                )
            serialized = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            revision_id = uuid.uuid4()
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
                    "SELECT :id, :next_revision_id, kind, object_key, sha256, content_type, size_bytes "
                    "FROM persona_assets WHERE persona_revision_id = :previous_revision_id"
                ),
                {
                    "id": uuid.uuid4(),
                    "next_revision_id": revision_id,
                    "previous_revision_id": source["revision_id"],
                },
            )
        else:
            revision_id = existing
        connection.execute(
            sa.text(
                "UPDATE guild_persona_assignments SET persona_revision_id = :next_revision_id "
                "WHERE persona_revision_id = :previous_revision_id"
            ),
            {
                "next_revision_id": revision_id,
                "previous_revision_id": source["revision_id"],
            },
        )
        connection.execute(
            sa.text(
                "UPDATE bot_application_profile SET persona_revision_id = :next_revision_id "
                "WHERE persona_revision_id = :previous_revision_id"
            ),
            {
                "next_revision_id": revision_id,
                "previous_revision_id": source["revision_id"],
            },
        )


def downgrade() -> None:
    connection = op.get_bind()
    for slug, previous_revision, next_revision in PERSONA_REVISIONS:
        revisions = (
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
        revision_ids = {int(row["revision"]): row["id"] for row in revisions}
        previous_id = revision_ids.get(previous_revision)
        next_id = revision_ids.get(next_revision)
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

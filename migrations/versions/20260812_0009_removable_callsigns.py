"""Add personality copy for removing reusable callsigns."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260812_0009"
down_revision: str | None = "20260812_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERSONA_REVISIONS = (
    ("walter_vigil", 23, 24),
    ("roy_velazco", 30, 31),
)
PERSONA_SCHEMA_VERSION = 22
CALLSIGN_COPY = {
    "walter_vigil": {
        "removed_title": "📻 Callsign eliminado",
        "removed_description": (
            "Su callsign dejó de estar guardado. Los próximos formularios de pager "
            "volverán a solicitarlo."
        ),
        "missing_description": "No tenía un callsign guardado para eliminar.",
        "remove_callsign_description": "Elimina su callsign operativo guardado",
        "help_line": "• `/quitar-callsign`: elimina el callsign guardado.",
    },
    "roy_velazco": {
        "removed_title": "📻 Callsign borrado",
        "removed_description": (
            "Listo, ya no quedó guardado. El próximo pager te lo va a pedir de nuevo."
        ),
        "missing_description": "No teníai ningún callsign guardado pa borrar.",
        "remove_callsign_description": "Borra el callsign q dejaste guardado",
        "help_line": "• `/quitar-callsign`: borra el callsign q dejaste guardado.",
    },
}


def upgrade() -> None:
    connection = op.get_bind()
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
        if existing is None:
            payload = dict(source["payload"])
            payload["revision"] = next_revision
            payload["schema_version"] = PERSONA_SCHEMA_VERSION
            copy = CALLSIGN_COPY[slug]
            callsigns = payload["messages"]["interface"]["callsigns"]
            callsigns["removed_title"] = copy["removed_title"]
            callsigns["removed_description"] = copy["removed_description"]
            callsigns["missing_description"] = copy["missing_description"]
            commands = payload["messages"]["interface"]["commands"]
            commands["remove_callsign_description"] = copy["remove_callsign_description"]
            help_copy = payload["messages"]["help"]
            general_commands = help_copy["general_commands"]
            if "/quitar-callsign" not in general_commands:
                marker = "• `/callsigns`:"
                help_line = copy["help_line"]
                help_copy["general_commands"] = general_commands.replace(
                    marker,
                    f"{help_line}\n{marker}",
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

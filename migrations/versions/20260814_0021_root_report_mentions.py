"""Allow report authors to request review in the root publication."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260814_0021"
down_revision: str | None = "20260814_0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERSONA_SCHEMA_VERSION = 31
PERSONA_REVISIONS = (
    ("walter_vigil", 34, 35),
    ("roy_velazco", 41, 42),
)
HELP_COPY = {
    "walter_vigil": {
        "owner_request_review": (
            "Si el reporte es suyo, puede mencionarme al publicarlo, responder directamente a "
            "su propia publicación y mencionarme después, o utilizarme dentro del hilo del "
            "expediente. Dispone de hasta {max_rounds} intervenciones propias para ese reporte."
        ),
        "owner_info_start": (
            "Mencione a {bot_mention} en el mismo mensaje al publicar el reporte. Si ya lo "
            "publicó, responda directamente a ese mensaje y mencióneme. Abriré o reutilizaré "
            "el hilo del expediente."
        ),
    },
    "roy_velazco": {
        "owner_request_review": (
            "si el reporte es tuyo, podís mencionarme al publicarlo, responder tu propia "
            "publicación y llamarme después, o hablarme dentro del hilo. tenís hasta "
            "{max_rounds} intervenciones propias pa ese reporte."
        ),
        "owner_info_start": (
            "menciona a {bot_mention} en el mismo mensaje cuando publiques el reporte. si ya "
            "lo mandaste, responde directamente esa publicación y llámame. voy a abrir o "
            "reutilizar el hilo de la cuestión."
        ),
    },
}


def upgrade() -> None:
    connection = op.get_bind()
    for slug, previous_revision, next_revision in PERSONA_REVISIONS:
        _install_persona_revision(
            connection,
            slug=slug,
            previous_revision=previous_revision,
            next_revision=next_revision,
        )


def downgrade() -> None:
    connection = op.get_bind()
    for slug, previous_revision, next_revision in PERSONA_REVISIONS:
        _restore_persona_revision(
            connection,
            slug=slug,
            previous_revision=previous_revision,
            next_revision=next_revision,
        )


def _install_persona_revision(
    connection: sa.Connection,
    *,
    slug: str,
    previous_revision: int,
    next_revision: int,
) -> None:
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
        return
    next_id = connection.execute(
        sa.text(
            "SELECT id FROM persona_revisions "
            "WHERE persona_id = :persona_id AND revision = :revision"
        ),
        {"persona_id": source["persona_id"], "revision": next_revision},
    ).scalar_one_or_none()
    if next_id is None:
        next_id = uuid.uuid4()
        payload = dict(source["payload"])
        payload["revision"] = next_revision
        payload["schema_version"] = PERSONA_SCHEMA_VERSION
        payload["messages"]["help"].update(HELP_COPY[slug])
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        connection.execute(
            sa.text(
                "INSERT INTO persona_revisions "
                "(id, persona_id, revision, schema_version, status, payload, checksum, published_at) "
                "VALUES (:id, :persona_id, :revision, :schema_version, 'published', "
                "CAST(:payload AS jsonb), :checksum, now())"
            ),
            {
                "id": next_id,
                "persona_id": source["persona_id"],
                "revision": next_revision,
                "schema_version": PERSONA_SCHEMA_VERSION,
                "payload": serialized.decode(),
                "checksum": hashlib.sha256(serialized).hexdigest(),
            },
        )
        _copy_assets(connection, source["revision_id"], next_id)
    _activate_revision(connection, source["revision_id"], next_id)


def _copy_assets(connection: sa.Connection, previous_id: uuid.UUID, next_id: uuid.UUID) -> None:
    assets = (
        connection.execute(
            sa.text(
                "SELECT kind, object_key, sha256, content_type, size_bytes "
                "FROM persona_assets WHERE persona_revision_id = :previous"
            ),
            {"previous": previous_id},
        )
        .mappings()
        .all()
    )
    for asset in assets:
        connection.execute(
            sa.text(
                "INSERT INTO persona_assets "
                "(id, persona_revision_id, kind, object_key, sha256, content_type, size_bytes) "
                "VALUES (:id, :next, :kind, :object_key, :sha256, :content_type, :size_bytes)"
            ),
            {"id": uuid.uuid4(), "next": next_id, **asset},
        )


def _activate_revision(
    connection: sa.Connection,
    previous_id: uuid.UUID,
    next_id: uuid.UUID,
) -> None:
    connection.execute(
        sa.text(
            "UPDATE guild_persona_assignments SET persona_revision_id = :next "
            "WHERE persona_revision_id = :previous"
        ),
        {"next": next_id, "previous": previous_id},
    )
    connection.execute(
        sa.text(
            "UPDATE bot_application_profile SET persona_revision_id = :next "
            "WHERE persona_revision_id = :previous"
        ),
        {"next": next_id, "previous": previous_id},
    )


def _restore_persona_revision(
    connection: sa.Connection,
    *,
    slug: str,
    previous_revision: int,
    next_revision: int,
) -> None:
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
        return
    _activate_revision(connection, next_id, previous_id)
    connection.execute(
        sa.text("DELETE FROM persona_revisions WHERE id = :next"),
        {"next": next_id},
    )

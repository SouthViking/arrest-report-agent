"""Accept an optional evidence bag reference in report-owner submissions."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260815_0025"
down_revision: str | None = "20260815_0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERSONA_SCHEMA_VERSION = 33
PERSONA_REVISIONS = (
    ("walter_vigil", 38, 39),
    ("roy_velazco", 45, 46),
)
PERSONA_COPY = {
    "walter_vigil": {
        "access": {
            "owner_invalid_report_messages": [
                "⚠️ **Complete el reporte antes de solicitar la revisión fuera del hilo.** "
                "Use **{bot_mention} Nombre Apellido** o **{bot_mention} Nombre Apellido - "
                "#NÚMERO** y adjunte al menos {min_images} imágenes: frontal, lateral, reporte "
                "de arresto y lista de delitos. También puede abrir el hilo de su publicación "
                "y mencionarme allí. Este intento no consumió una intervención.",
                "📎 **Faltan datos o recursos para iniciar fuera del hilo.** Use "
                "**{bot_mention} Nombre Apellido**, seguido de **- #NÚMERO** cuando exista una "
                "bolsa de evidencia, y adjunte un mínimo de {min_images} imágenes. Si el "
                "reporte ya está publicado, abra su hilo y mencióneme allí con normalidad.",
            ]
        },
        "help": {
            "owner_info_start": (
                "Para iniciar desde el canal o mediante un reply, use **{bot_mention} Nombre "
                "Apellido**. Si existe una bolsa de evidencia, también puede usar "
                "**{bot_mention} Nombre Apellido - #NÚMERO**. Adjunte el expediente; si ya lo "
                "publicó sin mencionarme, abra su hilo y llámeme allí con cualquier mensaje."
            )
        },
    },
    "roy_velazco": {
        "access": {
            "owner_invalid_report_messages": [
                "⚠️ **así no parte fuera del hilo po.** manda **{bot_mention} Nombre Apellido** "
                "o **{bot_mention} Nombre Apellido - #NÚMERO** y adjunta al menos {min_images} "
                "imágenes: frontal, lateral, reporte de arresto y delitos. si ya publicaste la "
                "cuestión, abre su hilo y llámame ahí. no te gasté una vuelta.",
                "📎 **te faltó armar la entrada fuera del hilo.** usa **{bot_mention} Nombre "
                "Apellido**, con **- #NÚMERO** si tenís bolsa de evidencia, y mínimo "
                "{min_images} imágenes; o abre el hilo de tu reporte y háblame ahí como persona "
                "normal.",
            ]
        },
        "help": {
            "owner_info_start": (
                "pa partir desde el canal o con un reply, usa **{bot_mention} Nombre Apellido**. "
                "si tenís bolsa de evidencia, también sirve **{bot_mention} Nombre Apellido - "
                "#NÚMERO**. adjunta el expediente; si ya lo publicaste sin llamarme, abre su "
                "hilo y mencióname ahí con cualquier mensaje."
            )
        },
    },
}


def upgrade() -> None:
    connection = op.get_bind()
    for slug, previous_revision, next_revision in PERSONA_REVISIONS:
        _install_persona_revision(connection, slug, previous_revision, next_revision)


def downgrade() -> None:
    connection = op.get_bind()
    for slug, previous_revision, next_revision in PERSONA_REVISIONS:
        _restore_persona_revision(connection, slug, previous_revision, next_revision)


def _install_persona_revision(
    connection: sa.Connection,
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
        payload["messages"]["access"].update(PERSONA_COPY[slug]["access"])
        payload["messages"]["help"].update(PERSONA_COPY[slug]["help"])
        serialized = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        connection.execute(
            sa.text(
                "INSERT INTO persona_revisions "
                "(id, persona_id, revision, schema_version, status, payload, checksum, "
                "published_at) VALUES (:id, :persona_id, :revision, :schema_version, "
                "'published', CAST(:payload AS jsonb), :checksum, now())"
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

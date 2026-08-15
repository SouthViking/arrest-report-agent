"""Allow report owners to start naturally from their canonical report thread."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260815_0024"
down_revision: str | None = "20260814_0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERSONA_SCHEMA_VERSION = 33
PERSONA_REVISIONS = (
    ("walter_vigil", 37, 38),
    ("roy_velazco", 44, 45),
)
PERSONA_COPY = {
    "walter_vigil": {
        "access": {
            "owner_invalid_report_messages": [
                "⚠️ **Complete el reporte antes de solicitar la revisión fuera del hilo.** "
                "Use exactamente **{bot_mention} Nombre Apellido** y adjunte al menos "
                "{min_images} imágenes: frontal, lateral, reporte de arresto y lista de delitos. "
                "También puede abrir el hilo de su publicación y mencionarme allí. Este intento "
                "no consumió una intervención.",
                "📎 **Faltan datos o recursos para iniciar fuera del hilo.** El mensaje debe "
                "seguir el formato **{bot_mention} Nombre Apellido** y contener un mínimo de "
                "{min_images} imágenes. Si el reporte ya está publicado, abra su hilo y "
                "mencióneme allí con normalidad.",
            ]
        },
        "help": {
            "owner_info_start": (
                "Para iniciar desde el canal o mediante un reply, use **{bot_mention} Nombre "
                "Apellido** y adjunte el expediente. Si publicó el reporte sin mencionarme, "
                "abra su hilo y llámeme allí con cualquier mensaje."
            ),
            "owner_info_requirements": (
                "Fuera del hilo se requieren al menos **4 imágenes**: fotografía frontal, "
                "fotografía lateral, reporte de arresto y lista de delitos. Dentro del hilo "
                "revisaré los recursos disponibles y solicitaré lo que falte."
            ),
            "owner_info_continue": (
                "Dentro del hilo, mencióneme o responda uno de mis mensajes con naturalidad. "
                "La primera ejecución y cada continuación consumen una de las intervenciones "
                "disponibles."
            ),
        },
    },
    "roy_velazco": {
        "access": {
            "owner_invalid_report_messages": [
                "⚠️ **así no parte fuera del hilo po.** manda exactamente **{bot_mention} "
                "Nombre Apellido** y adjunta al menos {min_images} imágenes: frontal, lateral, "
                "reporte de arresto y delitos. si ya publicaste la cuestión, abre su hilo y "
                "llámame ahí. no te gasté una vuelta.",
                "📎 **te faltó armar la entrada fuera del hilo.** usa **{bot_mention} Nombre "
                "Apellido** con mínimo {min_images} imágenes, o abre el hilo de tu reporte y "
                "háblame ahí como persona normal.",
            ]
        },
        "help": {
            "owner_info_start": (
                "pa partir desde el canal o con un reply, usa **{bot_mention} Nombre Apellido** "
                "y adjunta el expediente. si ya lo publicaste sin llamarme, abre su hilo y "
                "mencióname ahí con cualquier mensaje."
            ),
            "owner_info_requirements": (
                "fuera del hilo van mínimo **4 imágenes**: frontal, lateral, reporte de arresto "
                "y lista de delitos. dentro del hilo reviso lo que haya y te digo si falta algo."
            ),
            "owner_info_continue": (
                "ya dentro del hilo, mencióname o responde uno de mis mensajes como querai. "
                "la primera ejecución y cada continuación gastan una de tus intervenciones."
            ),
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
        sa.text("SELECT id FROM persona_revisions WHERE persona_id = :persona_id AND revision = :revision"),
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
    connection.execute(sa.text("DELETE FROM persona_revisions WHERE id = :next"), {"next": next_id})

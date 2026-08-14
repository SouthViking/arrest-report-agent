"""Publish compact self-review help for report authors."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260814_0020"
down_revision: str | None = "20260814_0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERSONA_SCHEMA_VERSION = 31
PERSONA_REVISIONS = (
    ("walter_vigil", 33, 34),
    ("roy_velazco", 40, 41),
)
INFO_COPY = {
    "walter_vigil": {
        "owner_info_intro": (
            "Si usted publicó el reporte, puede solicitar ayuda para revisarlo aunque no tenga "
            "acceso general al agente. La autorización se limita a su propio expediente."
        ),
        "owner_info_start": (
            "Responda directamente al mensaje donde publicó su reporte y mencione a "
            "{bot_mention}. Abriré o reutilizaré el hilo de ese expediente."
        ),
        "owner_info_continue": (
            "Dentro del hilo, mencióneme o responda uno de mis mensajes. Puede enviar "
            "correcciones, nuevas imágenes o archivos para continuar la revisión."
        ),
        "owner_info_limit": (
            "Dispone de hasta **{max_rounds} intervenciones por reporte**. Después deberá "
            "continuar personal autorizado."
        ),
        "owner_info_unavailable": (
            "El acceso directo para autores de reportes no está habilitado en este servidor."
        ),
        "command_description": "Explica cómo revisar tu propio reporte",
    },
    "roy_velazco": {
        "owner_info_intro": (
            "si tú publicaste el reporte, podís pedirme ayuda aunque no tengai acceso general. "
            "eso sí, solo dentro de tu propio expediente, no te emocionís."
        ),
        "owner_info_start": (
            "responde directamente el mensaje donde publicaste tu reporte y menciona a "
            "{bot_mention}. voy a abrir o reutilizar el hilo de esa cuestión."
        ),
        "owner_info_continue": (
            "ya dentro del hilo, mencióname o responde uno de mis mensajes. manda ahí las "
            "correcciones, imágenes o archivos nuevos."
        ),
        "owner_info_limit": (
            "tenís hasta **{max_rounds} intervenciones por reporte**. después tiene q seguir "
            "alguien autorizado."
        ),
        "owner_info_unavailable": (
            "el acceso directo pa autores de reportes no está habilitado en este server."
        ),
        "command_description": "Explica cómo revisar tu propio reporte",
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
        help_copy = payload["messages"]["help"]
        for key in (
            "owner_info_intro",
            "owner_info_start",
            "owner_info_continue",
            "owner_info_limit",
            "owner_info_unavailable",
        ):
            help_copy[key] = INFO_COPY[slug][key]
        payload["messages"]["interface"]["commands"]["info_reports_description"] = (
            INFO_COPY[slug]["command_description"]
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

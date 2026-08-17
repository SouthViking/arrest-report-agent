"""Publish MDC help and visibility copy for Walter and Roy."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260817_0030"
down_revision: str | None = "20260817_0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERSONA_SCHEMA_VERSION = 37
PERSONA_REVISIONS = (
    ("walter_vigil", 42, 43),
    ("roy_velazco", 49, 50),
)
PERSONA_COPY = {
    "walter_vigil": {
        "messages": {
            "info_description": (
                "El MDC permite consultar la información operativa disponible de un personaje "
                "sin exponer datos ajenos al servicio."
            ),
            "info_usage": (
                "Ejecute `/mdc`, escriba el **nombre exacto del personaje** y confirme la "
                "consulta desde el formulario."
            ),
            "info_visibility": (
                "Elija **Visible para todos** para publicar la ficha en el canal o "
                "**Solo para mí** para recibirla de forma privada."
            ),
            "info_results": (
                "La ficha reúne estado de custodia, órdenes activas, multas pendientes, "
                "vehículos, propiedades y antecedentes disponibles. Si una sección no "
                "responde, se indicará sin descartar el resto."
            ),
        },
        "description": "Explica cómo consultar el MDC y elegir su visibilidad",
        "guide": (
            "• `/info-mdc`: cómo consultar registros operativos y elegir su visibilidad."
        ),
    },
    "roy_velazco": {
        "messages": {
            "info_description": (
                "El MDC sirve pa revisar la ficha operativa disponible de un personaje sin "
                "andar inventando datos."
            ),
            "info_usage": (
                "Usa `/mdc`, escribe el **nombre exacto del personaje** en el formulario y "
                "manda la consulta."
            ),
            "info_visibility": (
                "Elige **Visible para todos** si querís dejar la ficha en el canal o "
                "**Solo para mí** si la necesitai privada."
            ),
            "info_results": (
                "Te muestra custodia, órdenes, multas, vehículos, propiedades y antecedentes "
                "disponibles. Si una parte falla, te aviso cuál fue y dejo el resto igual."
            ),
        },
        "description": "Explica cómo usar el MDC y decidir quién ve la ficha",
        "guide": "• `/info-mdc`: cómo buscar una ficha y decidir quién la ve.",
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
        copy = PERSONA_COPY[slug]
        payload["messages"]["interface"]["mdc"].update(copy["messages"])
        payload["messages"]["interface"]["commands"]["info_mdc_description"] = copy[
            "description"
        ]
        help_copy = payload["messages"]["help"]
        feature_lines = [
            line for line in help_copy["feature_guides"].splitlines() if "/info-mdc" not in line
        ]
        feature_lines.insert(-1, copy["guide"])
        help_copy["feature_guides"] = "\n".join(feature_lines)
        help_copy["quick_references"] = "\n".join(
            line for line in help_copy["quick_references"].splitlines() if "`/mdc`" not in line
        )
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

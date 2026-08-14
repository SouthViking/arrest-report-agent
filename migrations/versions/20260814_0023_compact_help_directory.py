"""Replace the expanded help catalogue with a compact feature directory."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260814_0023"
down_revision: str | None = "20260814_0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERSONA_SCHEMA_VERSION = 33
PERSONA_REVISIONS = (
    ("walter_vigil", 36, 37),
    ("roy_velazco", 43, 44),
)
LEGACY_HELP_FIELDS = (
    "review_scope_title",
    "review_scope",
    "location_title",
    "request_review_title",
    "request_review",
    "owner_request_review",
    "thread_behavior_title",
    "thread_behavior",
    "personal_commands_title",
    "personal_commands",
    "general_commands_title",
    "general_commands",
    "supervision_commands_title",
    "supervision_commands",
    "official_docs_title",
    "official_docs",
)
HELP_COPY = {
    "walter_vigil": {
        "description": (
            "Soy **{display_name}**, el **{role_title}** asignado a este turno. Selecciona la "
            "guía de la función que necesites para consultar sus instrucciones sin recorrer una "
            "lista interminable."
        ),
        "feature_guides_title": "🧭 Guías por función",
        "feature_guides": (
            "• `/info-reportes`: cómo solicitar y continuar la revisión de tu reporte.\n"
            "• `/info-roles`: cómo pedir altas o bajas de roles.\n"
            "• `/info-pager`: cómo emitir, responder y cerrar pagers.\n"
            "• `/info-callsigns`: cómo registrar y consultar callsigns.\n"
            "• `/info-sugerencias`: cómo enviar una sugerencia."
        ),
        "quick_references_title": "📚 Referencias rápidas",
        "quick_references": (
            "• `/guia`: consulta el manual de reportes por secciones.\n"
            "• `/cargos`: busca delitos y sanciones.\n"
            "• `/faq`: resuelve dudas frecuentes de la guía."
        ),
        "footer": "Elige la guía correspondiente y sigue sus indicaciones, {form_of_address}.",
    },
    "roy_velazco": {
        "description": (
            "soy **{display_name}**, **{role_title}** de este turno. elige la guía de la "
            "cuestión q necesitai y no te tragai una muralla de comandos."
        ),
        "feature_guides_title": "🧭 Guías por función",
        "feature_guides": (
            "• `/info-reportes`: cómo pedir y seguir la revisión de tu reporte.\n"
            "• `/info-roles`: cómo pedir q te agreguen o saquen roles.\n"
            "• `/info-pager`: cómo emitir, responder y cerrar pagers.\n"
            "• `/info-callsigns`: cómo guardar y buscar callsigns.\n"
            "• `/info-sugerencias`: cómo mandar una sugerencia."
        ),
        "quick_references_title": "📚 Referencias rápidas",
        "quick_references": (
            "• `/guia`: abre el manual de reportes por sección.\n"
            "• `/cargos`: busca delitos y sanciones sin inventar.\n"
            "• `/faq`: responde las dudas frecuentes de la guía."
        ),
        "footer": "elige la guía q corresponde antes de venir a adivinar, {form_of_address}.",
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
        sa.text("SELECT id FROM persona_revisions WHERE persona_id = :persona_id AND revision = :revision"),
        {"persona_id": source["persona_id"], "revision": next_revision},
    ).scalar_one_or_none()
    if next_id is None:
        next_id = uuid.uuid4()
        payload = dict(source["payload"])
        payload["revision"] = next_revision
        payload["schema_version"] = PERSONA_SCHEMA_VERSION
        help_copy = payload["messages"]["help"]
        for field in LEGACY_HELP_FIELDS:
            help_copy.pop(field, None)
        help_copy.update(HELP_COPY[slug])
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

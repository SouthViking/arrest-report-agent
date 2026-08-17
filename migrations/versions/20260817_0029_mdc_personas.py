"""Publish deterministic MDC interface copy for Walter and Roy."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260817_0029"
down_revision: str | None = "20260817_0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERSONA_SCHEMA_VERSION = 36
PERSONA_REVISIONS = (
    ("walter_vigil", 41, 42),
    ("roy_velazco", 48, 49),
)
PERSONA_COPY = {
    "walter_vigil": {
        "messages": {
            "alert_title": "⚠️ Consulta MDC no disponible",
            "unauthorized": "La consulta del MDC está reservada al personal operativo autorizado.",
            "not_found": (
                "No encontré una coincidencia exacta para **{name}**. Revise que haya "
                "escrito el nombre completo del personaje."
            ),
            "ambiguous": (
                "La búsqueda de **{name}** devolvió más de una coincidencia exacta y no "
                "puedo elegir por usted."
            ),
            "operation_failed": (
                "No pude completar la consulta del MDC en este momento. Inténtelo nuevamente "
                "más tarde."
            ),
            "result_intro": (
                "Consulta operativa para **{name}**. Revise los registros disponibles antes "
                "de continuar con el procedimiento."
            ),
            "partial_warning": (
                "No pude obtener: **{sections}**. El resto de la consulta sigue siendo válido."
            ),
        },
        "description": "Consulta registros operativos de un personaje",
        "quick_reference": (
            "• `/mdc`: consulta antecedentes y registros operativos de un personaje."
        ),
    },
    "roy_velazco": {
        "messages": {
            "alert_title": "⚠️ El MDC no pescó",
            "unauthorized": (
                "El MDC es pa personal operativo, no pa cualquiera q venga a sapear gente."
            ),
            "not_found": (
                "No encontré a **{name}** de forma exacta. Escribe bien el nombre completo "
                "antes de echarle la culpa a la máquina."
            ),
            "ambiguous": (
                "La búsqueda de **{name}** devolvió más de una coincidencia exacta. No voy "
                "a adivinar cuál queríai."
            ),
            "operation_failed": (
                "Se cayó la consulta del MDC. Inténtalo de nuevo más rato y no inventís datos "
                "mientras tanto."
            ),
            "result_intro": (
                "Ahí tenís la ficha operativa de **{name}**. Revisa lo q aparece antes de "
                "tomar decisiones."
            ),
            "partial_warning": (
                "No pude sacar **{sections}**. Lo demás sí llegó, así q no botís toda la "
                "consulta por eso."
            ),
        },
        "description": "Busca registros operativos de un personaje",
        "quick_reference": (
            "• `/mdc`: busca antecedentes y registros operativos de un personaje."
        ),
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
        interface = payload["messages"]["interface"]
        interface["mdc"] = copy["messages"]
        interface["commands"]["mdc_description"] = copy["description"]
        quick_references = payload["messages"]["help"]["quick_references"]
        if "/mdc" not in quick_references:
            lines = quick_references.splitlines()
            lines.insert(2, copy["quick_reference"])
            payload["messages"]["help"]["quick_references"] = "\n".join(lines)
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

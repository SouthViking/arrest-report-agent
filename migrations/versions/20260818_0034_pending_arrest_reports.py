"""Add deterministic pending arrest-report scans."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260818_0034"
down_revision: str | None = "20260818_0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FEATURE_KEY = "pending_arrest_reports"
LSSD_GUILD_ID = 1234674521416663070
LSSD_FACTION_ID = 3
PERSONA_SCHEMA_VERSION = 39
PERSONA_REVISIONS = (
    ("walter_vigil", 44, 45),
    ("roy_velazco", 51, 52),
)
PERSONA_COPY = {
    "walter_vigil": {
        "messages": {
            "alert_title": "⚠️ Reportes pendientes no disponibles",
            "unauthorized": "Esta consulta está reservada al personal operativo autorizado.",
            "unavailable": (
                "La consulta de reportes pendientes no está habilitada para este servidor."
            ),
            "operation_failed": (
                "No pude revisar los reportes de arresto en este momento. Inténtelo "
                "nuevamente más tarde."
            ),
            "invalid_range": (
                "Seleccione fechas válidas en formato AAAA-MM-DD, sin superar el período "
                "máximo permitido."
            ),
            "busy": "Ya hay una consulta de reportes pendientes en curso para este servidor.",
            "too_many_reports": (
                "El período contiene más de {limit} reportes. Seleccione un rango más acotado."
            ),
            "result_intro": "Estado de los reportes que todavía parecen incompletos.",
            "no_pending": (
                "No detecté reportes incompletos dentro del período seleccionado."
            ),
            "extension_warning": (
                "Los casos entre 24 y 48 horas pueden contar con una prórroga informada "
                "por otras vías."
            ),
            "other_query": "Solo quien inició la consulta puede cambiar esta página.",
        },
        "description": "Detecta reportes de arresto que parecen incompletos",
        "quick_reference": (
            "• `/reportes-pendientes`: detecta reportes de arresto que parecen incompletos."
        ),
    },
    "roy_velazco": {
        "messages": {
            "alert_title": "⚠️ No pude revisar los reportes",
            "unauthorized": (
                "Esta consulta es pa personal operativo, no pa cualquiera q quiera sapear "
                "reportes."
            ),
            "unavailable": (
                "No dejaron habilitada la consulta de reportes pendientes en este server."
            ),
            "operation_failed": (
                "No pude revisar los reportes ahora. Prueba de nuevo más rato."
            ),
            "invalid_range": (
                "Pon fechas válidas en formato AAAA-MM-DD y no te pasís del período permitido."
            ),
            "busy": (
                "Ya hay una revisión de reportes pendientes andando en este server. Espera "
                "q termine."
            ),
            "too_many_reports": (
                "Ese período tiene más de {limit} reportes. Acota las fechas pa no revisar "
                "media vida."
            ),
            "result_intro": "Ahí tenís los reportes q todavía parecen a medio hacer.",
            "no_pending": "No encontré reportes incompletos en el período q elegiste.",
            "extension_warning": (
                "Los casos entre 24 y 48 horas podrían tener una prórroga avisada por otra vía."
            ),
            "other_query": (
                "Esta consulta la abrió otra persona; deja q ella cambie la página."
            ),
        },
        "description": "Busca reportes de arresto q parecen incompletos",
        "quick_reference": (
            "• `/reportes-pendientes`: encuentra reportes de arresto q todavía parecen "
            "a medio hacer."
        ),
    },
}


def upgrade() -> None:
    connection = op.get_bind()
    _install_feature_configuration(connection)
    for slug, previous_revision, next_revision in PERSONA_REVISIONS:
        _install_persona_revision(connection, slug, previous_revision, next_revision)


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text("DELETE FROM guild_feature_settings WHERE feature_key = :key"),
        {"key": FEATURE_KEY},
    )
    for slug, previous_revision, next_revision in PERSONA_REVISIONS:
        _restore_persona_revision(connection, slug, previous_revision, next_revision)


def _install_feature_configuration(connection: sa.Connection) -> None:
    revisions = (
        connection.execute(sa.text("SELECT id, guild_id FROM guild_config_revisions"))
        .mappings()
        .all()
    )
    for config_revision in revisions:
        is_lssd = int(config_revision["guild_id"]) == LSSD_GUILD_ID
        payload = (
            {"faction_id": LSSD_FACTION_ID, "max_range_days": 90, "max_records": 1000}
            if is_lssd
            else {}
        )
        connection.execute(
            sa.text(
                "INSERT INTO guild_feature_settings "
                "(id, config_revision_id, feature_key, enabled, schema_version, payload) "
                "VALUES (:id, :revision_id, :key, :enabled, 1, CAST(:payload AS jsonb))"
            ),
            {
                "id": uuid.uuid4(),
                "revision_id": config_revision["id"],
                "key": FEATURE_KEY,
                "enabled": is_lssd,
                "payload": json.dumps(payload),
            },
        )


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
        interface["pending_arrest_reports"] = copy["messages"]
        interface["commands"]["pending_arrest_reports_description"] = copy["description"]
        help_copy = payload["messages"]["help"]
        quick_lines = [
            line
            for line in help_copy["quick_references"].splitlines()
            if "`/reportes-pendientes`" not in line
        ]
        quick_lines.append(copy["quick_reference"])
        help_copy["quick_references"] = "\n".join(quick_lines)
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

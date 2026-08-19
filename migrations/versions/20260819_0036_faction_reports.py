"""Add restricted faction bank and workforce reports."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260819_0036"
down_revision: str | None = "20260818_0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

POLICY_KEY = "faction_report_roles"
FEATURE_KEY = "faction_reports"
LSSD_GUILD_ID = 1234674521416663070
LSSD_FACTION_ID = 3
LSSD_ROLE_IDS = (
    1234678029662294047,  # Command Staff
    1234678238182244414,  # Executive Staff
)
PERSONA_SCHEMA_VERSION = 41
PERSONA_REVISIONS = (
    ("walter_vigil", 46, 47),
    ("roy_velazco", 53, 54),
)
PERSONA_COPY = {
    "walter_vigil": {
        "messages": {
            "alert_title": "⚠️ Informe de facción no disponible",
            "unauthorized": (
                "Este informe está reservado al Executive Staff y Command Staff autorizados."
            ),
            "unavailable": (
                "La generación de informes de facción no está habilitada para este servidor."
            ),
            "operation_failed": (
                "No pude preparar el informe de facción en este momento. Inténtelo "
                "nuevamente más tarde."
            ),
            "invalid_range": (
                "Seleccione fechas válidas en formato AAAA-MM-DD, sin superar el período "
                "máximo permitido."
            ),
            "busy": "Ya hay un informe de facción en preparación para este servidor.",
            "too_many_records": (
                "El período contiene más de {limit} movimientos bancarios. Seleccione un "
                "rango más acotado."
            ),
            "ready_title": "📊 Informe de facción preparado",
            "ready_description": (
                "Período bancario: **{date_from} — {date_to}**. Se incluyeron "
                "**{movements} movimientos** y **{members} miembros activos**; el archivo "
                "está visible únicamente para usted."
            ),
        },
        "description": "Genera el informe restringido de banco y miembros",
        "quick_reference": (
            "• `/informe-faccion`: genera el informe restringido de banco y miembros activos."
        ),
    },
    "roy_velazco": {
        "messages": {
            "alert_title": "⚠️ No pude armar el informe",
            "unauthorized": (
                "Este archivo es solo pa Executive Staff y Command Staff; no es una "
                "planilla pa andar repartiendo por el server."
            ),
            "unavailable": "No dejaron habilitado el informe de facción en este server.",
            "operation_failed": (
                "No pude armar el informe ahora. Prueba de nuevo más rato y no inventís "
                "cifras mientras tanto."
            ),
            "invalid_range": (
                "Pon fechas válidas en formato AAAA-MM-DD y no te pasís del período permitido."
            ),
            "busy": (
                "Ya hay alguien armando este informe. Espera q termine antes de pedir otro igual."
            ),
            "too_many_records": (
                "Ese período tiene más de {limit} movimientos. Acota las fechas antes q "
                "Excel se transforme en evidencia criminal."
            ),
            "ready_title": "📊 Informe listo",
            "ready_description": (
                "Banco entre **{date_from} y {date_to}**: **{movements} movimientos** y "
                "**{members} miembros activos**. El archivo te llegó solo a vo, así q cuídalo."
            ),
        },
        "description": "Arma el informe restringido de banco y personal",
        "quick_reference": (
            "• `/informe-faccion`: arma el informe restringido de banco y personal activo."
        ),
    },
}


def upgrade() -> None:
    connection = op.get_bind()
    _install_configuration(connection)
    for slug, previous_revision, next_revision in PERSONA_REVISIONS:
        _install_persona_revision(connection, slug, previous_revision, next_revision)


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text("DELETE FROM guild_feature_settings WHERE feature_key = :key"),
        {"key": FEATURE_KEY},
    )
    connection.execute(
        sa.text("DELETE FROM guild_access_policies WHERE policy_key = :key"),
        {"key": POLICY_KEY},
    )
    for slug, previous_revision, next_revision in PERSONA_REVISIONS:
        _restore_persona_revision(connection, slug, previous_revision, next_revision)


def _install_configuration(connection: sa.Connection) -> None:
    revisions = (
        connection.execute(sa.text("SELECT id, guild_id FROM guild_config_revisions"))
        .mappings()
        .all()
    )
    for config_revision in revisions:
        is_lssd = int(config_revision["guild_id"]) == LSSD_GUILD_ID
        policy_id = uuid.uuid4()
        connection.execute(
            sa.text(
                "INSERT INTO guild_access_policies "
                "(id, config_revision_id, policy_key, subject_type, mode) "
                "VALUES (:id, :revision_id, :key, 'role', :mode)"
            ),
            {
                "id": policy_id,
                "revision_id": config_revision["id"],
                "key": POLICY_KEY,
                "mode": "restricted" if is_lssd else "disabled",
            },
        )
        if is_lssd:
            for position, role_id in enumerate(LSSD_ROLE_IDS):
                connection.execute(
                    sa.text(
                        "INSERT INTO guild_access_policy_members "
                        "(id, policy_id, snowflake_id, position) "
                        "VALUES (:id, :policy_id, :snowflake_id, :position)"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "policy_id": policy_id,
                        "snowflake_id": role_id,
                        "position": position,
                    },
                )
        payload = (
            {"faction_id": LSSD_FACTION_ID, "max_range_days": 366, "max_bank_records": 10_000}
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
        interface["faction_reports"] = copy["messages"]
        interface["commands"]["faction_report_description"] = copy["description"]
        help_copy = payload["messages"]["help"]
        quick_lines = [
            line
            for line in help_copy["quick_references"].splitlines()
            if "`/informe-faccion`" not in line
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

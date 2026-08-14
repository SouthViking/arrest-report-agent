"""Add bounded report-owner access to the review agent."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260814_0019"
down_revision: str | None = "20260813_0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FEATURE_KEY = "report_owner_access"
LSSD_GUILD_ID = 1234674521416663070
PERSONA_SCHEMA_VERSION = 30
PERSONA_REVISIONS = (
    ("walter_vigil", 32, 33),
    ("roy_velazco", 39, 40),
)
OWNER_MESSAGES = {
    "walter_vigil": [
        "🕒 **Límite del expediente alcanzado.** Ya utilizó las {max_rounds} intervenciones "
        "disponibles como autor de este reporte. El expediente queda visible, pero deberá "
        "solicitar a personal autorizado que continúe la revisión.",
        "📁 **Sin rondas disponibles.** Las {max_rounds} intervenciones reservadas al "
        "propietario de este expediente ya fueron consumidas. A partir de aquí solo puede "
        "continuar personal con acceso autorizado.",
        "⛔ **Cupo de revisión agotado.** Este reporte ya recibió sus {max_rounds} ejecuciones "
        "para el autor. Un miembro autorizado deberá hacerse cargo de cualquier revisión adicional.",
    ],
    "roy_velazco": [
        "🕒 **se acabaron las vueltas, campeón.** ya usaste las {max_rounds} intervenciones "
        "de tu reporte. si querís seguir, busca a alguien con acceso autorizado.",
        "📁 **hasta aquí llegaste con este expediente.** las {max_rounds} rondas del autor ya "
        "se gastaron; ahora tiene q continuar alguien del personal autorizado.",
        "⛔ **no quedan intentos pa este reporte.** ocupaste las {max_rounds} intervenciones "
        "disponibles como autor. habla con un autorizado si todavía falta revisar algo.",
    ],
}
OWNER_REQUEST_HELP = {
    "walter_vigil": (
        "Si el reporte es suyo, responda directamente a su propia publicación y mencióneme, o "
        "utilíceme dentro del hilo de ese expediente. Dispone de hasta {max_rounds} intervenciones "
        "propias para ese reporte."
    ),
    "roy_velazco": (
        "si el reporte es tuyo, también podís responder tu propia publicación y mencionarme, o "
        "hablarme dentro de su hilo. tenís hasta {max_rounds} intervenciones propias pa ese reporte."
    ),
}
INFO_REPORTS_DESCRIPTIONS = {
    "walter_vigil": "Explica cómo solicitar y continuar una revisión",
    "roy_velazco": "Explica cómo pedir y seguir una revisión",
}


def upgrade() -> None:
    op.create_table(
        "report_owner_agent_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "guild_id",
            sa.BigInteger(),
            sa.ForeignKey("discord_guilds.discord_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("root_message_id", sa.BigInteger(), nullable=False),
        sa.Column("trigger_message_id", sa.BigInteger(), nullable=False),
        sa.Column("owner_user_id", sa.BigInteger(), nullable=False),
        sa.Column("thread_id", sa.BigInteger()),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("failed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('started','completed','failed')",
            name="ck_report_owner_agent_run_status",
        ),
        sa.UniqueConstraint("trigger_message_id", name="uq_report_owner_agent_run_trigger"),
    )
    op.create_index(
        "ix_report_owner_agent_run_usage",
        "report_owner_agent_runs",
        ["guild_id", "root_message_id", "owner_user_id", "started_at"],
    )
    connection = op.get_bind()
    _install_features(connection)
    _install_personas(connection)


def downgrade() -> None:
    connection = op.get_bind()
    _downgrade_personas(connection)
    connection.execute(
        sa.text("DELETE FROM guild_feature_settings WHERE feature_key = :feature_key"),
        {"feature_key": FEATURE_KEY},
    )
    op.drop_index("ix_report_owner_agent_run_usage", table_name="report_owner_agent_runs")
    op.drop_table("report_owner_agent_runs")


def _install_features(connection: sa.Connection) -> None:
    revisions = (
        connection.execute(
            sa.text(
                "SELECT id, guild_id FROM guild_config_revisions revision WHERE NOT EXISTS ("
                "SELECT 1 FROM guild_feature_settings feature "
                "WHERE feature.config_revision_id = revision.id AND feature.feature_key = :key)"
            ),
            {"key": FEATURE_KEY},
        )
        .mappings()
        .all()
    )
    feature_table = sa.table(
        "guild_feature_settings",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("config_revision_id", postgresql.UUID(as_uuid=True)),
        sa.column("feature_key", sa.String()),
        sa.column("enabled", sa.Boolean()),
        sa.column("schema_version", sa.Integer()),
        sa.column("payload", postgresql.JSONB()),
    )
    rows = [
        {
            "id": uuid.uuid4(),
            "config_revision_id": item["id"],
            "feature_key": FEATURE_KEY,
            "enabled": int(item["guild_id"]) == LSSD_GUILD_ID,
            "schema_version": 1,
            "payload": {"max_rounds": 5} if int(item["guild_id"]) == LSSD_GUILD_ID else {},
        }
        for item in revisions
    ]
    if rows:
        op.bulk_insert(feature_table, rows)


def _install_personas(connection: sa.Connection) -> None:
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
            payload["messages"]["access"]["owner_quota_exhausted_messages"] = OWNER_MESSAGES[slug]
            payload["messages"]["help"]["owner_request_review"] = OWNER_REQUEST_HELP[slug]
            payload["messages"]["interface"]["commands"]["info_reports_description"] = (
                INFO_REPORTS_DESCRIPTIONS[slug]
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
        connection.execute(
            sa.text(
                "UPDATE guild_persona_assignments SET persona_revision_id = :next "
                "WHERE persona_revision_id = :previous"
            ),
            {"next": next_id, "previous": source["revision_id"]},
        )
        connection.execute(
            sa.text(
                "UPDATE bot_application_profile SET persona_revision_id = :next "
                "WHERE persona_revision_id = :previous"
            ),
            {"next": next_id, "previous": source["revision_id"]},
        )


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


def _downgrade_personas(connection: sa.Connection) -> None:
    for slug, previous_revision, next_revision in PERSONA_REVISIONS:
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

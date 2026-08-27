"""Add persistent Field Training Program assistance requests."""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260826_0052"
down_revision: str | None = "20260826_0051"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FEATURE_KEY = "ftp_requests"
LSSD_GUILD_ID = 1234674521416663070
LSSD_OPTIONS = {
    "channel_id": 1234749125820420139,
    "command_role_ids": [1234678029662294047],
    "executive_role_ids": [1234678238182244414],
}
PERSONA_SCHEMA_VERSION = 46
PERSONA_REVISIONS = {
    "walter_vigil": (52, 53),
    "roy_velazco": (60, 61),
}
FTP_COPY = {
    "walter_vigil": {
        "alert_title": "⚠️ Solicitud FTP no disponible",
        "unavailable": "Las solicitudes de instrucción FTP no están habilitadas para este servidor.",
        "unauthorized_requester": (
            "Este comando está reservado a integrantes del FTP y al personal de "
            "supervisión, mando o dirección autorizado."
        ),
        "invalid_reason": "Explique claramente por qué necesita un instructor antes de enviar la solicitud.",
        "active_request": (
            "Ya tiene una solicitud pendiente. Espere a que sea atendida o cancélela "
            "antes de crear otra."
        ),
        "published": "La solicitud fue publicada y el personal del FTP ya fue notificado.",
        "unauthorized_acceptor": "Solo un FTO o personal superior autorizado puede tomar esta solicitud.",
        "requester_cannot_accept": (
            "No puede tomar su propia solicitud; debe ser atendida por otra persona autorizada."
        ),
        "unauthorized_canceller": (
            "Solo el autor, Command Staff o Executive Staff puede cancelar esta solicitud."
        ),
        "stale": "La solicitud ya cambió de estado o dejó de estar disponible.",
        "operation_failed": (
            "No pude procesar la solicitud FTP en este momento. Inténtelo nuevamente más tarde."
        ),
    },
    "roy_velazco": {
        "alert_title": "⚠️ La solicitud FTP no pescó",
        "unavailable": "No dejaron habilitadas las solicitudes del FTP en este server.",
        "unauthorized_requester": (
            "Esto es pa FTT, FTO, MFTO y jefatura autorizada. No vengai a pedir "
            "instructores si no tenís nada q ver con el FTP."
        ),
        "invalid_reason": "Explica bien pa qué necesitai un instructor. La gente no adivina todavía.",
        "active_request": (
            "Ya pediste un FTO po. Espera q alguien la tome o cancela la cuestión antes de "
            "tirar otra."
        ),
        "published": "Listo, ya avisé al FTP. Ahora espera sin spamear medio Discord.",
        "unauthorized_acceptor": (
            "No tenís un rol autorizado pa hacerte cargo. No apretís botones por deporte."
        ),
        "requester_cannot_accept": (
            "¿Pedís un instructor y después te elegís tú mismo? así no funciona la cuestión."
        ),
        "unauthorized_canceller": (
            "Esa solicitud no es tuya y tampoco erís Command ni Executive. Deja el botón quieto."
        ),
        "stale": "Llegaste tarde: esa solicitud ya cambió de estado o dejó de existir.",
        "operation_failed": (
            "Se trabó la solicitud FTP. Prueba de nuevo más rato y no armís otra a mano todavía."
        ),
    },
}
COMMAND_COPY = {
    "walter_vigil": "Solicita la asistencia de un instructor del FTP",
    "roy_velazco": "Pide la asistencia de un instructor del FTP",
}
QUICK_REFERENCES = {
    "walter_vigil": (
        "• `/ftp-conectados`: muestra el personal del FTP conectado.\n"
        "• `/solicitar-fto`: solicita asistencia de un instructor del FTP."
    ),
    "roy_velazco": (
        "• `/ftp-conectados`: muestra la gente del FTP q está conectada.\n"
        "• `/solicitar-fto`: pide ayuda de un instructor del FTP."
    ),
}


def upgrade() -> None:
    op.create_table(
        "ftp_training_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "guild_id",
            sa.BigInteger(),
            sa.ForeignKey("discord_guilds.discord_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("channel_id", sa.BigInteger(), nullable=False),
        sa.Column("message_id", sa.BigInteger()),
        sa.Column("requester_user_id", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("claimed_by_user_id", sa.BigInteger()),
        sa.Column("resolved_by_user_id", sa.BigInteger()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint(
            "status IN ('publishing','pending','claimed','cancelled','failed')",
            name="ck_ftp_training_request_status",
        ),
        sa.CheckConstraint(
            "char_length(reason) BETWEEN 10 AND 500",
            name="ck_ftp_training_request_reason_length",
        ),
        sa.UniqueConstraint(
            "guild_id",
            "channel_id",
            "message_id",
            name="uq_ftp_training_request_message",
        ),
    )
    op.create_index(
        "ix_ftp_training_request_guild_status",
        "ftp_training_requests",
        ["guild_id", "status", "created_at"],
    )
    op.create_index(
        "uq_ftp_training_request_live_member",
        "ftp_training_requests",
        ["guild_id", "requester_user_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('publishing','pending')"),
    )
    connection = op.get_bind()
    _install_feature(connection)
    for slug, revisions in PERSONA_REVISIONS.items():
        _install_persona_revision(connection, slug, *revisions)


def downgrade() -> None:
    connection = op.get_bind()
    for slug, revisions in PERSONA_REVISIONS.items():
        _remove_persona_revision(connection, slug, *revisions)
    connection.execute(
        sa.text("DELETE FROM guild_feature_settings WHERE feature_key = :key"),
        {"key": FEATURE_KEY},
    )
    op.drop_index("uq_ftp_training_request_live_member", table_name="ftp_training_requests")
    op.drop_index("ix_ftp_training_request_guild_status", table_name="ftp_training_requests")
    op.drop_table("ftp_training_requests")


def _install_feature(connection: sa.Connection) -> None:
    rows = connection.execute(
        sa.text(
            "SELECT revision.id, revision.guild_id, status.enabled AS ftp_status_enabled "
            "FROM guild_config_revisions revision "
            "LEFT JOIN guild_feature_settings status "
            "ON status.config_revision_id = revision.id AND status.feature_key = 'ftp_status'"
        )
    ).mappings().all()
    for row in rows:
        enabled = int(row["guild_id"]) == LSSD_GUILD_ID and bool(row["ftp_status_enabled"])
        connection.execute(
            sa.text(
                "INSERT INTO guild_feature_settings "
                "(id, config_revision_id, feature_key, enabled, schema_version, payload) "
                "VALUES (:id, :revision_id, :key, :enabled, 1, CAST(:payload AS jsonb))"
            ),
            {
                "id": uuid.uuid4(),
                "revision_id": row["id"],
                "key": FEATURE_KEY,
                "enabled": enabled,
                "payload": json.dumps(LSSD_OPTIONS if enabled else {}),
            },
        )


def _install_persona_revision(
    connection: sa.Connection,
    slug: str,
    previous_revision: int,
    next_revision: int,
) -> None:
    source = connection.execute(
        sa.text(
            "SELECT persona.id AS persona_id, revision.id AS revision_id, revision.payload "
            "FROM personas persona JOIN persona_revisions revision ON revision.persona_id = persona.id "
            "WHERE persona.slug = :slug AND revision.revision = :revision"
        ),
        {"slug": slug, "revision": previous_revision},
    ).mappings().one_or_none()
    if source is None:
        return
    next_id = connection.execute(
        sa.text(
            "SELECT id FROM persona_revisions WHERE persona_id = :persona_id AND revision = :revision"
        ),
        {"persona_id": source["persona_id"], "revision": next_revision},
    ).scalar_one_or_none()
    if next_id is None:
        next_id = uuid.uuid4()
        payload = copy.deepcopy(dict(source["payload"]))
        payload["revision"] = next_revision
        payload["schema_version"] = PERSONA_SCHEMA_VERSION
        messages = payload["messages"]
        messages["interface"]["ftp_requests"] = FTP_COPY[slug]
        messages["interface"]["commands"]["ftp_request_description"] = COMMAND_COPY[slug]
        quick_references = str(messages["help"]["quick_references"])
        connected_marker = "\n• `/recursos`:"
        insertion = f"\n{QUICK_REFERENCES[slug]}"
        messages["help"]["quick_references"] = quick_references.replace(
            connected_marker,
            f"{insertion}{connected_marker}",
            1,
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


def _remove_persona_revision(
    connection: sa.Connection,
    slug: str,
    previous_revision: int,
    next_revision: int,
) -> None:
    rows = connection.execute(
        sa.text(
            "SELECT revision.revision, revision.id FROM personas persona "
            "JOIN persona_revisions revision ON revision.persona_id = persona.id "
            "WHERE persona.slug = :slug AND revision.revision IN (:previous, :next)"
        ),
        {"slug": slug, "previous": previous_revision, "next": next_revision},
    ).mappings().all()
    ids = {int(row["revision"]): row["id"] for row in rows}
    previous_id = ids.get(previous_revision)
    next_id = ids.get(next_revision)
    if previous_id is None or next_id is None:
        return
    _activate_revision(connection, next_id, previous_id)
    connection.execute(
        sa.text("DELETE FROM persona_revisions WHERE id = :revision_id"),
        {"revision_id": next_id},
    )


def _copy_assets(connection: sa.Connection, previous_id: uuid.UUID, next_id: uuid.UUID) -> None:
    assets = connection.execute(
        sa.text(
            "SELECT kind, object_key, sha256, content_type, size_bytes "
            "FROM persona_assets WHERE persona_revision_id = :previous"
        ),
        {"previous": previous_id},
    ).mappings().all()
    for asset in assets:
        connection.execute(
            sa.text(
                "INSERT INTO persona_assets "
                "(id, persona_revision_id, kind, object_key, sha256, content_type, size_bytes) "
                "VALUES (:id, :next, :kind, :object_key, :sha256, :content_type, :size_bytes)"
            ),
            {"id": uuid.uuid4(), "next": next_id, **asset},
        )


def _activate_revision(connection: sa.Connection, previous_id: uuid.UUID, next_id: uuid.UUID) -> None:
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

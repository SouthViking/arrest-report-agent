"""Add structured FTP requests and FTT entry welcomes."""

from __future__ import annotations

import copy
import hashlib
import json
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260921_0070"
down_revision: str | None = "20260921_0069"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LSSD_GUILD_ID = 1234674521416663070
FEATURE_KEY = "ftp_requests"
ROLE_REQUEST_FEATURE_KEY = "role_requests"
OLD_CHANNEL_ID = 1234749125820420139
FTP_REQUEST_CHANNEL_ID = 1551692208929906701
FTP_CHAT_CHANNEL_ID = 1234749125820420139
ANNOUNCEMENTS_CHANNEL_ID = 1234675521560772628
BIG_BROTHER_CHANNEL_ID = 1467761647551774852
FTP_RULES_URL = "https://sheriff.sarp.es/viewtopic.php?t=37"
PERSONA_SCHEMA_VERSION = 47
PERSONA_REVISIONS = {
    "walter_vigil": (54, 55),
    "roy_velazco": (62, 63),
}
PERSONA_COPY = {
    "walter_vigil": {
        "unauthorized_requester": (
            "Este comando está reservado a miembros con el rol Field Training Trainee."
        ),
        "wrong_channel": ("Utilice este comando en {channel}, el canal habilitado para las fichas del FTP."),
        "invalid_schedule": ("Indique una fecha y hora futuras válidas, con el formato DD/MM/AAAA y HH:MM."),
        "claim_success": "La ficha quedó asignada a su nombre y el trainee fue notificado.",
        "cancel_success": "La solicitud quedó cancelada correctamente.",
        "info_intro": (
            "Utilice este sistema para programar una ficha del FTP y notificar al instructor correspondiente."
        ),
        "info_closing": "Solo los Field Training Trainees pueden crear solicitudes.",
        "request_description": "Programa una ficha del Field Training Program",
        "info_description": "Explica cómo solicitar una ficha del FTP",
        "quick_reference": "• `/solicitar-fto`: programa una ficha del FTP.",
    },
    "roy_velazco": {
        "unauthorized_requester": (
            "Esto es pa los Field Training Trainees. Si no tenís FTT no podís pedir una ficha por acá, po."
        ),
        "wrong_channel": (
            "Las fichas se piden en {channel}. Anda al canal correcto antes de llenar medio "
            "Discord de solicitudes, criatura."
        ),
        "invalid_schedule": (
            "Esa fecha no sirve. Pon una fecha futura como DD/MM/AAAA y la hora como HH:MM, no jeroglíficos."
        ),
        "claim_success": (
            "Listo, la ficha quedó a tu nombre y ya le avisé al trainee. Ahora coordinen sin "
            "hacer una teleserie."
        ),
        "cancel_success": "Ya, solicitud cancelada. Al menos apretaste el botón correcto.",
        "info_intro": (
            "¿Necesitai agendar una ficha? Hazlo ordenado por acá y deja de perseguir "
            "instructores por medio Discord."
        ),
        "info_closing": (
            "Solo los Field Training Trainees pueden crear solicitudes. Sí, SOLO ELLOS, no "
            "apretís por curiosidad."
        ),
        "request_description": "Programa una ficha del Field Training Program",
        "info_description": "Explica cómo solicitar una ficha del FTP",
        "quick_reference": "• `/solicitar-fto`: programa una ficha del FTP.",
    },
}


def upgrade() -> None:
    op.add_column("ftp_training_requests", sa.Column("training_phase", sa.String(20)))
    op.add_column(
        "ftp_training_requests",
        sa.Column("requested_instructor_level", sa.String(20)),
    )
    op.add_column(
        "ftp_training_requests",
        sa.Column("scheduled_for", sa.DateTime(timezone=True)),
    )
    op.drop_constraint(
        "ck_ftp_training_request_reason_length",
        "ftp_training_requests",
        type_="check",
    )
    op.create_check_constraint(
        "ck_ftp_training_request_reason_length",
        "ftp_training_requests",
        "char_length(reason) <= 500",
    )
    op.create_check_constraint(
        "ck_ftp_training_request_phase",
        "ftp_training_requests",
        "training_phase IS NULL OR training_phase IN ('phase_1','phase_2','phase_3')",
    )
    op.create_check_constraint(
        "ck_ftp_training_request_instructor_level",
        "ftp_training_requests",
        "requested_instructor_level IS NULL OR requested_instructor_level IN ('general','supervisor')",
    )

    connection = op.get_bind()
    connection.execute(
        sa.text(
            """
            UPDATE guild_feature_settings AS feature
            SET payload = feature.payload || jsonb_build_object(
                'channel_id', CAST(:channel_id AS bigint)
            )
            FROM guild_config_revisions AS revision
            WHERE revision.id = feature.config_revision_id
              AND revision.guild_id = :guild_id
              AND feature.feature_key = :feature_key
            """
        ),
        {
            "guild_id": LSSD_GUILD_ID,
            "feature_key": FEATURE_KEY,
            "channel_id": FTP_REQUEST_CHANNEL_ID,
        },
    )
    connection.execute(
        sa.text(
            """
            UPDATE guild_feature_settings AS feature
            SET payload = jsonb_set(
                feature.payload,
                '{ftt_entry,welcome}',
                CAST(:welcome AS jsonb),
                true
            )
            FROM guild_config_revisions AS revision
            WHERE revision.id = feature.config_revision_id
              AND revision.guild_id = :guild_id
              AND feature.feature_key = :feature_key
              AND feature.payload ? 'ftt_entry'
            """
        ),
        {
            "guild_id": LSSD_GUILD_ID,
            "feature_key": ROLE_REQUEST_FEATURE_KEY,
            "welcome": json.dumps(
                {
                    "channel_id": FTP_CHAT_CHANNEL_ID,
                    "rules_url": FTP_RULES_URL,
                    "reference_channel_ids": [
                        ANNOUNCEMENTS_CHANNEL_ID,
                        BIG_BROTHER_CHANNEL_ID,
                    ],
                }
            ),
        },
    )
    for slug, revisions in PERSONA_REVISIONS.items():
        _install_persona_revision(connection, slug, *revisions)


def downgrade() -> None:
    connection = op.get_bind()
    for slug, revisions in PERSONA_REVISIONS.items():
        _remove_persona_revision(connection, slug, *revisions)
    connection.execute(
        sa.text(
            """
            UPDATE guild_feature_settings AS feature
            SET payload = jsonb_set(
                feature.payload,
                '{ftt_entry}',
                (feature.payload -> 'ftt_entry') - 'welcome'
            )
            FROM guild_config_revisions AS revision
            WHERE revision.id = feature.config_revision_id
              AND revision.guild_id = :guild_id
              AND feature.feature_key = :feature_key
              AND feature.payload ? 'ftt_entry'
            """
        ),
        {
            "guild_id": LSSD_GUILD_ID,
            "feature_key": ROLE_REQUEST_FEATURE_KEY,
        },
    )
    connection.execute(
        sa.text(
            """
            UPDATE guild_feature_settings AS feature
            SET payload = feature.payload || jsonb_build_object(
                'channel_id', CAST(:channel_id AS bigint)
            )
            FROM guild_config_revisions AS revision
            WHERE revision.id = feature.config_revision_id
              AND revision.guild_id = :guild_id
              AND feature.feature_key = :feature_key
            """
        ),
        {
            "guild_id": LSSD_GUILD_ID,
            "feature_key": FEATURE_KEY,
            "channel_id": OLD_CHANNEL_ID,
        },
    )
    connection.execute(
        sa.text(
            "UPDATE ftp_training_requests SET reason = 'Solicitud estructurada de ficha FTP.' "
            "WHERE char_length(reason) < 10"
        )
    )
    op.drop_constraint(
        "ck_ftp_training_request_instructor_level",
        "ftp_training_requests",
        type_="check",
    )
    op.drop_constraint(
        "ck_ftp_training_request_phase",
        "ftp_training_requests",
        type_="check",
    )
    op.drop_constraint(
        "ck_ftp_training_request_reason_length",
        "ftp_training_requests",
        type_="check",
    )
    op.create_check_constraint(
        "ck_ftp_training_request_reason_length",
        "ftp_training_requests",
        "char_length(reason) BETWEEN 10 AND 500",
    )
    op.drop_column("ftp_training_requests", "scheduled_for")
    op.drop_column("ftp_training_requests", "requested_instructor_level")
    op.drop_column("ftp_training_requests", "training_phase")


def _install_persona_revision(
    connection: sa.Connection,
    slug: str,
    previous_revision: int,
    next_revision: int,
) -> None:
    source = (
        connection.execute(
            sa.text(
                "SELECT persona.id AS persona_id, revision.id AS revision_id, revision.payload "
                "FROM personas persona "
                "JOIN persona_revisions revision ON revision.persona_id = persona.id "
                "WHERE persona.slug = :slug AND revision.revision = :revision"
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
        payload = copy.deepcopy(dict(source["payload"]))
        payload["revision"] = next_revision
        payload["schema_version"] = PERSONA_SCHEMA_VERSION
        copy_values = PERSONA_COPY[slug]
        ftp = payload["messages"]["interface"]["ftp_requests"]
        ftp.pop("invalid_reason", None)
        ftp.update(
            {
                "unauthorized_requester": copy_values["unauthorized_requester"],
                "wrong_channel": copy_values["wrong_channel"],
                "invalid_schedule": copy_values["invalid_schedule"],
                "claim_success": copy_values["claim_success"],
                "cancel_success": copy_values["cancel_success"],
                "info_intro": copy_values["info_intro"],
                "info_closing": copy_values["info_closing"],
            }
        )
        commands = payload["messages"]["interface"]["commands"]
        commands["ftp_request_description"] = copy_values["request_description"]
        commands["info_ftp_description"] = copy_values["info_description"]
        quick_lines = []
        for line in str(payload["messages"]["help"]["quick_references"]).splitlines():
            quick_lines.append(copy_values["quick_reference"] if "`/solicitar-fto`" in line else line)
        payload["messages"]["help"]["quick_references"] = "\n".join(quick_lines)
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


def _remove_persona_revision(
    connection: sa.Connection,
    slug: str,
    previous_revision: int,
    next_revision: int,
) -> None:
    rows = (
        connection.execute(
            sa.text(
                "SELECT revision.revision, revision.id FROM personas persona "
                "JOIN persona_revisions revision ON revision.persona_id = persona.id "
                "WHERE persona.slug = :slug AND revision.revision IN (:previous, :next)"
            ),
            {"slug": slug, "previous": previous_revision, "next": next_revision},
        )
        .mappings()
        .all()
    )
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

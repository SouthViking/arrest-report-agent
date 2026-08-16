"""Add the empty manual quiz catalogue and its persona-facing interface."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260815_0026"
down_revision: str | None = "20260815_0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERSONA_SCHEMA_VERSION = 34
PERSONA_REVISIONS = (
    ("walter_vigil", 39, 40),
    ("roy_velazco", 46, 47),
)
PERSONA_COPY = {
    "walter_vigil": {
        "quick_reference": "• `/pregunta-manual`: practica el contenido del manual.",
        "command": "Practica el contenido del manual de la facción",
        "interface": {
            "alert_title": "⚠️ Entrenamiento no disponible",
            "unauthorized": "Este ejercicio está reservado al personal operativo autorizado.",
            "no_questions": "Todavía no hay preguntas activas para este servidor.",
            "active_question": (
                "Ya publicó una pregunta pendiente. Espere a que alguien la responda o a que "
                "expire antes de solicitar otra."
            ),
            "cooldown": "Espere {seconds} segundos antes de solicitar otra pregunta.",
            "already_answered": "La pregunta ya fue respondida.",
            "operation_failed": "No pude preparar o corregir la pregunta en este momento.",
            "question_intro": (
                "Revise el caso y seleccione la respuesta que considere correcta. La primera "
                "respuesta cerrará el ejercicio."
            ),
            "selector_placeholder": "Seleccione una respuesta",
            "correct_messages": [
                "Respuesta correcta. Buen criterio.",
                "Correcto. Ese es el procedimiento esperado.",
                "Bien resuelto. Puede continuar con la siguiente cuando esté listo.",
            ],
            "incorrect_messages": [
                "La respuesta no es correcta. Revise la explicación antes de intentarlo "
                "nuevamente.",
                "Esa alternativa no corresponde. Tome nota del fundamento indicado.",
                "No es la opción esperada. El detalle de la corrección queda a continuación.",
            ],
        },
    },
    "roy_velazco": {
        "quick_reference": "• `/pregunta-manual`: demuestra si leíste algo del manual.",
        "command": "Prueba cuánto cachai del manual de la facción",
        "interface": {
            "alert_title": "⚠️ Entrenamiento no disponible",
            "unauthorized": (
                "Esto es pa personal operativo, no pa cualquiera q venga a apretar botones."
            ),
            "no_questions": (
                "Todavía no cargaron preguntas activas, así q por ahora te salvaste."
            ),
            "active_question": (
                "Ya tiraste una pregunta. Espera q alguien la responda o q venza antes de "
                "pedir otra."
            ),
            "cooldown": (
                "Espera {seconds} segundos antes de pedir otra, no es una metralleta de "
                "preguntas."
            ),
            "already_answered": "Llegaste tarde, esa pregunta ya la respondieron.",
            "operation_failed": (
                "Se trabó la cuestión y no pude preparar o corregir la pregunta."
            ),
            "question_intro": (
                "A ver cuánto del manual se les quedó... el primero q responda cierra la cuestión."
            ),
            "selector_placeholder": "Elige una respuesta",
            "correct_messages": [
                "Bien po, esa sí te la sabíai.",
                "Correcto. Al menos una parte del manual no la pasaste de largo.",
                "Buena, esa era. Podís pedir otra cuando estés listo.",
            ],
            "incorrect_messages": [
                "No po, esa no era. Revisa la explicación antes de inventar de nuevo.",
                "Le erraste. Lee el fundamento q está abajo y acuérdate pa la próxima.",
                "Mala respuesta. Por suerte esto era práctica; revisa la corrección.",
            ],
        },
    },
}


def upgrade() -> None:
    op.create_table(
        "manual_quiz_questions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "guild_id",
            sa.BigInteger(),
            sa.ForeignKey("discord_guilds.discord_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("slug", sa.String(length=120), nullable=False),
        sa.Column("answer_mode", sa.String(length=30), nullable=False),
        sa.Column("category", sa.String(length=30), nullable=False),
        sa.Column("difficulty", sa.String(length=20), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column("source_reference", sa.String(length=300), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="draft", nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_by", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "slug ~ '^[a-z0-9]+(_[a-z0-9]+)*$'",
            name="ck_manual_quiz_slug_format",
        ),
        sa.CheckConstraint(
            "answer_mode IN ('single_choice','true_false','multiple_select','short_answer',"
            "'ordering','matching')",
            name="ck_manual_quiz_answer_mode",
        ),
        sa.CheckConstraint(
            "category IN ('general','radio_code','procedure','scenario','identify_error',"
            "'legal','reports')",
            name="ck_manual_quiz_category",
        ),
        sa.CheckConstraint(
            "difficulty IN ('basic','intermediate','advanced')",
            name="ck_manual_quiz_difficulty",
        ),
        sa.CheckConstraint(
            "status IN ('draft','active','archived')",
            name="ck_manual_quiz_status",
        ),
        sa.CheckConstraint("revision > 0", name="ck_manual_quiz_revision_positive"),
        sa.CheckConstraint(
            "char_length(prompt) BETWEEN 1 AND 1500",
            name="ck_manual_quiz_prompt_length",
        ),
        sa.CheckConstraint(
            "char_length(explanation) BETWEEN 1 AND 1500",
            name="ck_manual_quiz_explanation_length",
        ),
        sa.CheckConstraint(
            "char_length(source_reference) BETWEEN 1 AND 300",
            name="ck_manual_quiz_source_length",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("guild_id", "slug", name="uq_manual_quiz_guild_slug"),
    )
    op.create_index(
        "ix_manual_quiz_guild_status_mode",
        "manual_quiz_questions",
        ["guild_id", "status", "answer_mode"],
    )
    op.create_table(
        "manual_quiz_options",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "question_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("manual_quiz_questions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("text", sa.String(length=300), nullable=False),
        sa.Column("is_correct", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.CheckConstraint(
            "position BETWEEN 0 AND 24",
            name="ck_manual_quiz_option_position",
        ),
        sa.CheckConstraint(
            "char_length(text) BETWEEN 1 AND 300",
            name="ck_manual_quiz_option_text_length",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "question_id",
            "position",
            name="uq_manual_quiz_option_position",
        ),
    )
    op.create_index(
        "ix_manual_quiz_option_question",
        "manual_quiz_options",
        ["question_id", "position"],
    )

    connection = op.get_bind()
    for slug, previous_revision, next_revision in PERSONA_REVISIONS:
        _install_persona_revision(connection, slug, previous_revision, next_revision)


def downgrade() -> None:
    connection = op.get_bind()
    for slug, previous_revision, next_revision in PERSONA_REVISIONS:
        _restore_persona_revision(connection, slug, previous_revision, next_revision)
    op.drop_index("ix_manual_quiz_option_question", table_name="manual_quiz_options")
    op.drop_table("manual_quiz_options")
    op.drop_index("ix_manual_quiz_guild_status_mode", table_name="manual_quiz_questions")
    op.drop_table("manual_quiz_questions")


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
        payload["messages"]["help"]["quick_references"] = (
            f'{payload["messages"]["help"]["quick_references"]}\n'
            f'{PERSONA_COPY[slug]["quick_reference"]}'
        )
        payload["messages"]["interface"]["manual_quiz"] = PERSONA_COPY[slug]["interface"]
        payload["messages"]["interface"]["commands"]["manual_question_description"] = (
            PERSONA_COPY[slug]["command"]
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

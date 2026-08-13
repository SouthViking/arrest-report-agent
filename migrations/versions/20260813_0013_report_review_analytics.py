"""Persist structured report reviews and expose deterministic error analytics."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260813_0013"
down_revision: str | None = "20260812_0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERSONA_REVISIONS = (
    ("walter_vigil", 27, 28),
    ("roy_velazco", 34, 35),
)
PERSONA_SCHEMA_VERSION = 26
ISSUE_TYPES = (
    ("deputy_identity", "Identidad del deputy", "Nombre o identidad del deputy ausente o incorrecta."),
    ("callsign_missing", "Callsign ausente", "El reporte no identifica el callsign del deputy."),
    ("callsign_format", "Formato del callsign", "El callsign se presenta con un formato mejorable."),
    ("orthography", "Ortografía y puntuación", "Errores de escritura, tildes o puntuación."),
    (
        "facts_insufficient",
        "Hechos insuficientes",
        "Los hechos no permiten comprender o justificar el procedimiento.",
    ),
    (
        "chronology_or_location",
        "Cronología o ubicación",
        "La secuencia temporal o el lugar presentan una deficiencia.",
    ),
    (
        "description_mismatch",
        "Descripción inconsistente",
        "Una descripción expresa no coincide con el material presentado.",
    ),
    (
        "evidence_reference",
        "Referencia de evidencia",
        "Falta o es inconsistente la referencia de evidencia física.",
    ),
    (
        "charge_not_justified",
        "Delito sin justificación",
        "Los hechos no justifican razonablemente un delito declarado.",
    ),
    (
        "charge_mismatch",
        "Delito no aplicable",
        "Un delito declarado contradice o no corresponde a los hechos.",
    ),
    (
        "required_material_missing",
        "Material obligatorio ausente",
        "Falta material requerido para el alcance de la revisión.",
    ),
    (
        "report_format",
        "Formato del reporte",
        "La estructura o formato obligatorio del reporte presenta un problema.",
    ),
    ("other", "Otro", "Hallazgo que no corresponde razonablemente a otra categoría."),
)


def upgrade() -> None:
    op.create_table(
        "report_issue_types",
        sa.Column("code", sa.String(50), primary_key=True),
        sa.Column("display_name", sa.String(100), nullable=False),
        sa.Column("description", sa.String(500), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("position", name="uq_report_issue_type_position"),
    )
    issue_table = sa.table(
        "report_issue_types",
        sa.column("code", sa.String()),
        sa.column("display_name", sa.String()),
        sa.column("description", sa.String()),
        sa.column("position", sa.Integer()),
    )
    op.bulk_insert(
        issue_table,
        [
            {
                "code": code,
                "display_name": display_name,
                "description": description,
                "position": position,
            }
            for position, (code, display_name, description) in enumerate(ISSUE_TYPES)
        ],
    )
    op.create_table(
        "report_reviews",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "guild_id",
            sa.BigInteger(),
            sa.ForeignKey("discord_guilds.discord_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("parent_channel_id", sa.BigInteger(), nullable=False),
        sa.Column("thread_id", sa.BigInteger()),
        sa.Column("root_message_id", sa.BigInteger(), nullable=False),
        sa.Column("trigger_message_id", sa.BigInteger(), nullable=False),
        sa.Column("report_author_user_id", sa.BigInteger()),
        sa.Column("requester_user_id", sa.BigInteger(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("review_scope", sa.String(20), nullable=False),
        sa.Column("verdict", sa.String(30), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("reasoning_effort", sa.String(30), nullable=False),
        sa.Column("prompt_version", sa.String(40), nullable=False),
        sa.Column("persona_id", sa.String(80), nullable=False),
        sa.Column("persona_revision", sa.Integer(), nullable=False),
        sa.Column("app_version", sa.String(30), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("failed_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("attempt_number > 0", name="ck_report_review_attempt_positive"),
        sa.CheckConstraint(
            "review_scope IN ('full','narrative','charges','targeted')",
            name="ck_report_review_scope",
        ),
        sa.CheckConstraint(
            "verdict IN ('approved','corrections_required','inconclusive')",
            name="ck_report_review_verdict",
        ),
        sa.CheckConstraint(
            "status IN ('publishing','published','failed')",
            name="ck_report_review_status",
        ),
        sa.UniqueConstraint("guild_id", "trigger_message_id", name="uq_report_review_trigger"),
        sa.UniqueConstraint(
            "guild_id",
            "root_message_id",
            "attempt_number",
            name="uq_report_review_attempt",
        ),
    )
    op.create_index(
        "ix_report_review_guild_published_created",
        "report_reviews",
        ["guild_id", "status", "created_at"],
    )
    op.create_index(
        "ix_report_review_guild_root",
        "report_reviews",
        ["guild_id", "root_message_id", "attempt_number"],
    )
    op.create_table(
        "report_review_findings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "review_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("report_reviews.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "issue_code",
            sa.String(50),
            sa.ForeignKey("report_issue_types.code", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("severity", sa.String(20), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "severity IN ('required','warning','observation')",
            name="ck_report_review_finding_severity",
        ),
        sa.CheckConstraint("position >= 0", name="ck_report_review_finding_position"),
        sa.UniqueConstraint("review_id", "position", name="uq_report_review_finding_position"),
    )
    op.create_index(
        "ix_report_review_finding_issue",
        "report_review_findings",
        ["issue_code", "severity"],
    )
    _install_personas(op.get_bind())


def downgrade() -> None:
    _downgrade_personas(op.get_bind())
    op.drop_index("ix_report_review_finding_issue", table_name="report_review_findings")
    op.drop_table("report_review_findings")
    op.drop_index("ix_report_review_guild_root", table_name="report_reviews")
    op.drop_index("ix_report_review_guild_published_created", table_name="report_reviews")
    op.drop_table("report_reviews")
    op.drop_table("report_issue_types")


def _install_personas(connection: sa.Connection) -> None:
    persona_dir = Path(__file__).resolve().parents[2] / "src" / "arrest_report_agent" / "config" / "personas"
    if not persona_dir.exists():
        return
    for slug, previous_revision, next_revision in PERSONA_REVISIONS:
        source = (
            connection.execute(
                sa.text(
                    "SELECT p.id AS persona_id, pr.id AS revision_id "
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
        payload = json.loads((persona_dir / f"{slug}.json").read_text(encoding="utf-8"))
        if (
            int(payload["revision"]) != next_revision
            or int(payload["schema_version"]) != PERSONA_SCHEMA_VERSION
        ):
            raise RuntimeError(f"Packaged persona {slug} does not match review analytics migration")
        existing = connection.execute(
            sa.text(
                "SELECT id FROM persona_revisions WHERE persona_id = :persona_id AND revision = :revision"
            ),
            {"persona_id": source["persona_id"], "revision": next_revision},
        ).scalar_one_or_none()
        revision_id = existing or uuid.uuid4()
        if existing is None:
            serialized = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            connection.execute(
                sa.text(
                    "INSERT INTO persona_revisions "
                    "(id, persona_id, revision, schema_version, status, payload, checksum, published_at) "
                    "VALUES (:id, :persona_id, :revision, :schema_version, 'published', "
                    "CAST(:payload AS jsonb), :checksum, now())"
                ),
                {
                    "id": revision_id,
                    "persona_id": source["persona_id"],
                    "revision": next_revision,
                    "schema_version": PERSONA_SCHEMA_VERSION,
                    "payload": serialized.decode(),
                    "checksum": hashlib.sha256(serialized).hexdigest(),
                },
            )
            connection.execute(
                sa.text(
                    "INSERT INTO persona_assets "
                    "(id, persona_revision_id, kind, object_key, sha256, content_type, size_bytes) "
                    "SELECT :id, :next_revision, kind, object_key, sha256, content_type, size_bytes "
                    "FROM persona_assets WHERE persona_revision_id = :previous_revision"
                ),
                {
                    "id": uuid.uuid4(),
                    "next_revision": revision_id,
                    "previous_revision": source["revision_id"],
                },
            )
        connection.execute(
            sa.text(
                "UPDATE guild_persona_assignments SET persona_revision_id = :next_revision "
                "WHERE persona_revision_id = :previous_revision"
            ),
            {"next_revision": revision_id, "previous_revision": source["revision_id"]},
        )
        connection.execute(
            sa.text(
                "UPDATE bot_application_profile SET persona_revision_id = :next_revision "
                "WHERE persona_revision_id = :previous_revision"
            ),
            {"next_revision": revision_id, "previous_revision": source["revision_id"]},
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

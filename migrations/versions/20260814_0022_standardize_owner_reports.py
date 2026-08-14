"""Standardize owner report submissions before granting limited agent access."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "20260814_0022"
down_revision: str | None = "20260814_0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERSONA_SCHEMA_VERSION = 32
PERSONA_REVISIONS = (
    ("walter_vigil", 35, 36),
    ("roy_velazco", 42, 43),
)
PERSONA_COPY = {
    "walter_vigil": {
        "access": {
            "owner_invalid_report_messages": [
                "⚠️ **Complete el reporte antes de solicitar la revisión.** Use exactamente "
                "**{bot_mention} Nombre Apellido** y adjunte al menos {min_images} imágenes: "
                "frontal, lateral, reporte de arresto y lista de delitos. Este intento no "
                "consumió una intervención.",
                "📎 **Faltan datos o recursos del expediente.** El mensaje debe seguir el "
                "formato **{bot_mention} Nombre Apellido** y contener un mínimo de "
                "{min_images} imágenes: frontal, lateral, reporte de arresto y delitos. "
                "Corríjalo y vuelva a intentarlo.",
            ]
        },
        "help": {
            "owner_request_review": (
                "Si el reporte es suyo, publíquelo con el formato **@mención Nombre Apellido** "
                "y sus cuatro imágenes mínimas. También puede responder directamente a esa "
                "publicación o utilizarme dentro del hilo. Dispone de hasta {max_rounds} "
                "intervenciones propias para ese reporte."
            ),
            "owner_info_start": (
                "Publique el mensaje usando exactamente: **{bot_mention} Nombre Apellido**. "
                "Si ya lo publicó con ese formato, responda directamente a ese mensaje y "
                "mencióneme. Abriré o reutilizaré el hilo del expediente."
            ),
            "owner_info_requirements": (
                "Adjunte al menos **4 imágenes**: fotografía frontal, fotografía lateral, "
                "reporte de arresto y lista de delitos. La revisión no comenzará ni consumirá "
                "una intervención si falta el formato o el material mínimo."
            ),
        },
    },
    "roy_velazco": {
        "access": {
            "owner_invalid_report_messages": [
                "⚠️ **así no parte la revisión po.** manda exactamente **{bot_mention} "
                "Nombre Apellido** y adjunta al menos {min_images} imágenes: frontal, lateral, "
                "reporte de arresto y delitos. no te gasté una vuelta por este intento.",
                "📎 **te faltó armar el expediente.** el formato es **{bot_mention} Nombre "
                "Apellido** y van mínimo {min_images} imágenes: frontal, lateral, reporte de "
                "arresto y lista de delitos. arréglalo y vuelve a llamarme.",
            ]
        },
        "help": {
            "owner_request_review": (
                "si el reporte es tuyo, publícalo como **@mención Nombre Apellido** con sus "
                "cuatro imágenes mínimas. también podís responder esa publicación o hablarme "
                "dentro del hilo. tenís hasta {max_rounds} intervenciones propias pa ese reporte."
            ),
            "owner_info_start": (
                "manda el mensaje exactamente así: **{bot_mention} Nombre Apellido**. si ya "
                "lo publicaste con ese formato, responde directamente esa publicación y "
                "llámame. voy a abrir o reutilizar el hilo de la cuestión."
            ),
            "owner_info_requirements": (
                "adjunta mínimo **4 imágenes**: frontal, lateral, reporte de arresto y lista "
                "de delitos. si falta el formato o el material, no parte la revisión ni te "
                "gasto una intervención."
            ),
        },
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
        payload["messages"]["access"].update(PERSONA_COPY[slug]["access"])
        payload["messages"]["help"].update(PERSONA_COPY[slug]["help"])
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

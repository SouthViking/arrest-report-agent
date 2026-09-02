"""Remove the retired scope-refusal copy from persisted persona revisions."""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op

revision: str = "20260902_0061"
down_revision: str | None = "20260901_0060"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    connection = op.get_bind()
    rows = list(
        connection.execute(
            sa.text(
                "SELECT id, persona_id, payload, checksum "
                "FROM persona_revisions ORDER BY persona_id, revision"
            )
        ).mappings()
    )

    updates: list[tuple[object, dict[str, Any], str]] = []
    final_owners: dict[tuple[str, str], object] = {}
    for row in rows:
        payload = copy.deepcopy(dict(row["payload"]))
        changed = _remove_scope_refusal(payload)
        final_checksum = _payload_checksum(payload)
        uniqueness_key = (str(row["persona_id"]), final_checksum)
        existing_owner = final_owners.setdefault(uniqueness_key, row["id"])
        if existing_owner != row["id"]:
            raise RuntimeError(
                "Removing messages.access.scope_refusal would create duplicate persona revisions "
                f"{existing_owner} and {row['id']}"
            )
        if changed:
            updates.append((row["id"], payload, final_checksum))

    # Move affected rows through unique temporary checksums so the migration is safe even when
    # one row's final checksum happens to equal another affected row's previous checksum.
    occupied_checksums = {str(row["checksum"]) for row in rows}
    for revision_id, _payload, _final_checksum in updates:
        salt = 0
        while True:
            temporary_checksum = hashlib.sha256(f"{revision}:{revision_id}:{salt}".encode()).hexdigest()
            if temporary_checksum not in occupied_checksums:
                break
            salt += 1
        occupied_checksums.add(temporary_checksum)
        connection.execute(
            sa.text("UPDATE persona_revisions SET checksum = :checksum WHERE id = :revision_id"),
            {"revision_id": revision_id, "checksum": temporary_checksum},
        )

    for revision_id, payload, final_checksum in updates:
        connection.execute(
            sa.text(
                "UPDATE persona_revisions "
                "SET payload = CAST(:payload AS jsonb), checksum = :checksum "
                "WHERE id = :revision_id"
            ),
            {
                "revision_id": revision_id,
                "payload": json.dumps(payload, ensure_ascii=False),
                "checksum": final_checksum,
            },
        )


def downgrade() -> None:
    # The removed copy is retired behavior and cannot be reconstructed reliably for every
    # historical revision. Older code must not be redeployed after this data cleanup.
    pass


def _remove_scope_refusal(payload: dict[str, Any]) -> bool:
    messages = payload.get("messages")
    if not isinstance(messages, dict):
        return False
    access = messages.get("access")
    if not isinstance(access, dict) or "scope_refusal" not in access:
        return False
    del access["scope_refusal"]
    return True


def _payload_checksum(payload: dict[str, Any]) -> str:
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(serialized).hexdigest()

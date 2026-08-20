"""Persist manual quiz shuffle cycles and install advanced scenario questions."""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
import zlib
from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260820_0042"
down_revision: str | None = "20260820_0041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CATALOG_ZLIB_BASE64 = (
    "eNrFfVuP40aW5l8h/CxXu7s9O2vnw6I6q3pcmGpXdWfZ+zAYCCEyUsk0xZAZpGzVYH7F/gI/+qEeGvWwgLHAAqM/tucaF4qilE5n"
    "L9ANV6ZSZEScS5zLd875t//4xDfD+pMvPzHVzrSlrZa3rivt0pR9vbPLzvra9/jB0nhvvd/Ytv9k8Ylp/Q+2W25cZeG7vm7XjV2W"
    "d64uLXxamt6uXbfHj0rbmq528Nuqvr2ty6Hp98nr4Pfbzm22Pfzum7aobG/bunJFvW5dZ4rDx66yrfXFznYr01i/KDam7Wv4XdEY"
    "jz84+LD29Qo+LPZF6woDazTvTdHWhbcF78AWt4cPvi7xs94+K/7r//51OPxc2J1pBlPWh49tYbYNfF5s7L3rCngHPK6yxeBNVwxt"
    "0R9+8rb7H7Bc++O2Ma3pa9fCml/jN2ChlfVuZStYWFkbXMXKwLk9K17DIk23waVaXGpje9xGYX+s17bV1dGX6MgN7GFoTdjE1sDD"
    "bQPP3w79vnBFb4E8nfPPYCneDUirzt7aDp6BpPiLaQfTFK9vbl4U//W/i1evnhVvXXP40MPePP7mG+9wXxNrgge6LW7Lf/Llv/3H"
    "J739EYly7eC44ZldUboW/993rhFywFo7y2fYXRW9q8zu8IF2Dwd/P/i+BoKb9sTLStd1toR39N1g/3MR3vi8gqf3dUfncPil6euN"
    "gRe+x5NBasBpMDUKXxdb29EJLortAMfajCnhnSeOgnM1+MebGnihBNahpXXw96XbuAk6pAu8NY1PV3htNqsaVmKKlTXtyqyLLfCM"
    "H4Ad9/CPikWmNovCDO33A7Iqnlw1lD3SFzbQgnA1RAdlVlfcmT2c1Q6kxVRu7vXPh9519XskCdD6+6FmBiwaON7K4UFXtRszHJwV"
    "sLG366GtnLJTkKUSjqzrh25i2/+Ob55WEqvGVHZZqYYI8v9U2gGIubXwHGBhZEZkqwLW0ILYJpLO9CNBh8PWAzZBcpBDULKBuwKl"
    "CqSSsgd+4rasFZJDDFoDTmfoYA1bWDnSAQm6Ibk71g8vQWbqrhxaeZGnN5kKX9TSm2DZyLq9Sd+1QJZR5gLqbUnGUckVJrA/Kiki"
    "ajwWPJLq+HQy9jRNiYf7j1Igf+JdsOIGUbQtLVD4TdRdqohh8XhYsLeKJEcoOKMx3tF5LHCbeLqwUwPPruoNvKx3hbuFb9liA38V"
    "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"
    "IiDrxTmlhv79/wHQkDYC"
)


def upgrade() -> None:
    op.create_table(
        "manual_quiz_rotations",
        sa.Column(
            "guild_id",
            sa.BigInteger(),
            sa.ForeignKey("discord_guilds.discord_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("answer_mode_filter", sa.String(length=30), nullable=False),
        sa.Column("difficulty_filter", sa.String(length=20), nullable=False),
        sa.Column("cycle", sa.Integer(), server_default="1", nullable=False),
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
            "answer_mode_filter IN ('any','single_choice','true_false')",
            name="ck_manual_quiz_rotation_answer_mode",
        ),
        sa.CheckConstraint(
            "difficulty_filter IN ('any','basic','intermediate','advanced')",
            name="ck_manual_quiz_rotation_difficulty",
        ),
        sa.CheckConstraint("cycle > 0", name="ck_manual_quiz_rotation_cycle_positive"),
        sa.PrimaryKeyConstraint(
            "guild_id",
            "answer_mode_filter",
            "difficulty_filter",
        ),
    )
    op.create_table(
        "manual_quiz_rotation_entries",
        sa.Column("guild_id", sa.BigInteger(), nullable=False),
        sa.Column("answer_mode_filter", sa.String(length=30), nullable=False),
        sa.Column("difficulty_filter", sa.String(length=20), nullable=False),
        sa.Column("cycle", sa.Integer(), nullable=False),
        sa.Column(
            "question_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("manual_quiz_questions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "presented_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "cycle > 0",
            name="ck_manual_quiz_rotation_entry_cycle_positive",
        ),
        sa.ForeignKeyConstraint(
            ["guild_id", "answer_mode_filter", "difficulty_filter"],
            [
                "manual_quiz_rotations.guild_id",
                "manual_quiz_rotations.answer_mode_filter",
                "manual_quiz_rotations.difficulty_filter",
            ],
            name="fk_manual_quiz_rotation_entry_scope",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "guild_id",
            "answer_mode_filter",
            "difficulty_filter",
            "cycle",
            "question_id",
        ),
    )
    op.create_index(
        "ix_manual_quiz_rotation_entry_scope",
        "manual_quiz_rotation_entries",
        [
            "guild_id",
            "answer_mode_filter",
            "difficulty_filter",
            "cycle",
        ],
    )
    _install_advanced_questions(op.get_bind())


def downgrade() -> None:
    connection = op.get_bind()
    slugs = tuple(question["slug"] for question in _catalog())
    if slugs:
        connection.execute(
            sa.text(
                "DELETE FROM manual_quiz_questions WHERE slug IN :slugs"
            ).bindparams(sa.bindparam("slugs", expanding=True)),
            {"slugs": slugs},
        )
    op.drop_index(
        "ix_manual_quiz_rotation_entry_scope",
        table_name="manual_quiz_rotation_entries",
    )
    op.drop_table("manual_quiz_rotation_entries")
    op.drop_table("manual_quiz_rotations")


def _catalog() -> tuple[dict[str, Any], ...]:
    serialized = zlib.decompress(base64.b64decode(CATALOG_ZLIB_BASE64)).decode("utf-8")
    payload = json.loads(serialized)
    if not isinstance(payload, list):
        raise RuntimeError("manual quiz migration catalogue must be a JSON array")
    return tuple(payload)


def _install_advanced_questions(connection: sa.Connection) -> None:
    guild_ids = connection.execute(
        sa.text("SELECT DISTINCT guild_id FROM manual_quiz_questions")
    ).scalars()
    for guild_id in guild_ids:
        for question in _catalog():
            question_id = _stable_uuid(f"{guild_id}:{question['slug']}")
            connection.execute(
                sa.text(
                    "INSERT INTO manual_quiz_questions "
                    "(id, guild_id, slug, answer_mode, category, difficulty, prompt, "
                    "explanation, source_reference, status, revision) VALUES "
                    "(:id, :guild_id, :slug, :answer_mode, :category, :difficulty, :prompt, "
                    ":explanation, :source_reference, 'active', 1) "
                    "ON CONFLICT (guild_id, slug) DO UPDATE SET "
                    "answer_mode = EXCLUDED.answer_mode, category = EXCLUDED.category, "
                    "difficulty = EXCLUDED.difficulty, prompt = EXCLUDED.prompt, "
                    "explanation = EXCLUDED.explanation, "
                    "source_reference = EXCLUDED.source_reference, status = 'active', "
                    "revision = manual_quiz_questions.revision + 1, updated_at = now() "
                    "WHERE ROW(manual_quiz_questions.answer_mode, "
                    "manual_quiz_questions.category, manual_quiz_questions.difficulty, "
                    "manual_quiz_questions.prompt, manual_quiz_questions.explanation, "
                    "manual_quiz_questions.source_reference, manual_quiz_questions.status) "
                    "IS DISTINCT FROM ROW(EXCLUDED.answer_mode, EXCLUDED.category, "
                    "EXCLUDED.difficulty, EXCLUDED.prompt, EXCLUDED.explanation, "
                    "EXCLUDED.source_reference, EXCLUDED.status)"
                ),
                {
                    "id": question_id,
                    "guild_id": guild_id,
                    "slug": question["slug"],
                    "answer_mode": question["answer_mode"],
                    "category": question["category"],
                    "difficulty": question["difficulty"],
                    "prompt": question["prompt"],
                    "explanation": question["explanation"],
                    "source_reference": question["source_reference"],
                },
            )
            connection.execute(
                sa.text("DELETE FROM manual_quiz_options WHERE question_id = :question_id"),
                {"question_id": question_id},
            )
            connection.execute(
                sa.text(
                    "INSERT INTO manual_quiz_options "
                    "(id, question_id, position, text, is_correct) VALUES "
                    "(:id, :question_id, :position, :text, :is_correct)"
                ),
                [
                    {
                        "id": _stable_uuid(f"{guild_id}:{question['slug']}:{position}"),
                        "question_id": question_id,
                        "position": position,
                        "text": option["text"],
                        "is_correct": option["correct"],
                    }
                    for position, option in enumerate(question["options"])
                ],
            )


def _stable_uuid(value: str) -> uuid.UUID:
    return uuid.UUID(hashlib.md5(value.encode(), usedforsecurity=False).hexdigest())

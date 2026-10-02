from datetime import UTC, datetime, timedelta

import discord
import pytest

from arrest_report_agent.ftp_requests.coordination import build_coordination_embed
from arrest_report_agent.ftp_requests.models import FtpTrainingPhase


@pytest.mark.parametrize("with_block", [False, True])
def test_coordination_card_has_same_layout_and_native_schedule(with_block):
    start = datetime(2026, 10, 3, 22, tzinfo=UTC)
    end = start + timedelta(hours=1)
    embed = build_coordination_embed(
        trainee_id=200,
        instructor_id=300,
        phase=FtpTrainingPhase.PHASE_2,
        starts_at=start,
        ends_at=end if with_block else None,
        block_position=2 if with_block else None,
        comments="Practicar detenciones.",
    )
    block = " · Bloque 2" if with_block else ""
    schedule = f"<t:{int(start.timestamp())}:f>"
    if with_block:
        schedule += f" → <t:{int(end.timestamp())}:t>"
    assert embed.title == "🤝 Ficha coordinada"
    assert embed.color == discord.Color.blue()
    assert embed.description == (
        f"**Fase:** Fase 2{block}\n**Horario:** {schedule}\n**Trainee:** <@200>\n**Instructor:** <@300>"
    )
    assert len(embed.fields) == 1
    assert embed.fields[0].name == "Comentarios del trainee"
    assert embed.fields[0].value == "Practicar detenciones."
    assert not embed.fields[0].inline
    assert embed.footer.text == "La ficha quedó asignada. Coordinen entre ustedes para realizarla."


@pytest.mark.parametrize(
    "comments",
    ["", " \n ", "@everyone @here <@123456789012345678> <@&987654321098765432> **texto**", "*" * 500],
)
def test_coordination_comments_are_optional_safe_and_within_discord_limits(comments):
    embed = build_coordination_embed(
        trainee_id=200,
        instructor_id=300,
        phase=FtpTrainingPhase.PHASE_1,
        starts_at=datetime.now(UTC),
        comments=comments,
    )
    if not comments.strip():
        assert not embed.fields
    else:
        value = embed.fields[0].value
        assert len(value) <= 1024
        assert value == discord.utils.escape_mentions(discord.utils.escape_markdown(comments))
        assert all(
            mention not in value
            for mention in ("@everyone", "@here", "<@123456789012345678>", "<@&987654321098765432>")
        )


def test_legacy_request_confirmation_does_not_invent_phase_or_schedule():
    embed = build_coordination_embed(
        trainee_id=200,
        instructor_id=300,
        phase=None,
        starts_at=None,
    )
    assert "**Fase:** Por coordinar" in embed.description
    assert "**Horario:** Por coordinar" in embed.description
    assert "Bloque" not in embed.description
    assert "<t:" not in embed.description
    assert not embed.fields

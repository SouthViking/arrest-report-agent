"""Shared confirmation card for claimed requests and reserved instructor blocks."""

from datetime import datetime

import discord

from .models import FtpTrainingPhase


def build_coordination_embed(
    *,
    trainee_id: int | None,
    instructor_id: int,
    phase: FtpTrainingPhase | None,
    starts_at: datetime | None,
    ends_at: datetime | None = None,
    block_position: int | None = None,
    comments: str = "",
) -> discord.Embed:
    phase_label = phase.label if phase is not None else "Por coordinar"
    if block_position is not None:
        phase_label += f" · Bloque {block_position}"
    schedule = "Por coordinar"
    if starts_at is not None:
        schedule = f"<t:{int(starts_at.timestamp())}:f>"
        if ends_at is not None:
            schedule += f" → <t:{int(ends_at.timestamp())}:t>"
    trainee = f"<@{trainee_id}>" if trainee_id is not None else "Por asignar"
    embed = discord.Embed(
        title="🤝 Ficha coordinada",
        color=discord.Color.blue(),
        description=(
            f"**Fase:** {phase_label}\n**Horario:** {schedule}\n"
            f"**Trainee:** {trainee}\n**Instructor:** <@{instructor_id}>"
        ),
    )
    if comments := comments.strip():
        embed.add_field(
            name="Comentarios del trainee",
            value=discord.utils.escape_mentions(discord.utils.escape_markdown(comments)),
            inline=False,
        )
    embed.set_footer(text="La ficha quedó asignada. Coordinen entre ustedes para realizarla.")
    return embed

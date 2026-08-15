"""Discord rendering for public cost snapshots."""

from __future__ import annotations

from decimal import Decimal

import discord

from .models import CostMonitoringSnapshot


def build_cost_monitoring_embed(snapshot: CostMonitoringSnapshot) -> discord.Embed:
    """Render only values actually returned by the configured providers."""

    embed = discord.Embed(
        title="💰 Estado de consumo",
        color=discord.Color.gold(),
        timestamp=snapshot.fetched_at,
    )
    if snapshot.openai is not None:
        openai = snapshot.openai
        openai_lines = [f"**Gasto del mes:** `{_usd(openai.current_spend)}`"]
        if openai.hard_limit is not None:
            scope = "proyecto" if openai.limit_scope == "project" else "organización"
            openai_lines.append(f"**Límite de {scope}:** `{_usd(openai.hard_limit)}`")
        if openai.remaining is not None:
            openai_lines.append(f"**Margen disponible:** `{_usd(openai.remaining)}`")
        embed.add_field(name="🤖 OpenAI", value="\n".join(openai_lines), inline=False)

    if snapshot.railway is not None:
        railway = snapshot.railway
        railway_lines: list[str] = []
        if railway.current_usage is not None:
            railway_lines.append(f"**Uso actual:** `{_usd(railway.current_usage)}`")
        if railway.estimated_bill is not None:
            railway_lines.append(f"**Factura estimada:** `{_usd(railway.estimated_bill)}`")
        if railway.hard_limit is not None:
            railway_lines.append(f"**Límite de gasto:** `{_usd(railway.hard_limit)}`")
        if railway.remaining is not None:
            railway_lines.append(f"**Margen disponible:** `{_usd(railway.remaining)}`")
        if railway_lines:
            embed.add_field(name="🚂 Railway", value="\n".join(railway_lines), inline=False)

    embed.set_footer(text="Datos actualizados")
    return embed


def _usd(value: Decimal) -> str:
    rendered = f"{value:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
    return f"USD {rendered}"

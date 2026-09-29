"""Compact Discord fleet presentation without assumptions about metric units."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from math import ceil

import discord

from arrest_report_agent.infrastructure.sarp.models import SarpFactionVehicle

from .service import AlprFilter, FleetSnapshot, FleetView

PAGE_SIZE = 8
VIEW_LABELS = {
    FleetView.SUMMARY: "📋 Resumen de flota",
    FleetView.HEALTH: "🔧 Menor salud registrada",
    FleetView.FUEL: "⛽ Menor combustible",
    FleetView.OIL: "🛢️ Menor aceite",
}


def safe_text(value: str) -> str:
    return discord.utils.escape_markdown(discord.utils.escape_mentions(value.replace("\n", " ")))


def metric(value: float | None) -> str:
    if value is None:
        return "Sin dato"
    return f"{value:,.2f}".rstrip("0").rstrip(".").translate(str.maketrans(",.", ".,"))


def driver_label(vehicle: SarpFactionVehicle, drivers: Mapping[str, int]) -> str:
    name = (vehicle.last_driver or "").strip()
    if name in drivers:
        return f"<@{drivers[name]}>"
    return safe_text(name) if name else "Sin registro"


def flag_label(value: bool | None, yes: str, no: str) -> str:
    return "Sin dato" if value is None else yes if value else no


def vehicle_embed(vehicle: SarpFactionVehicle, drivers: Mapping[str, int]) -> discord.Embed:
    embed = discord.Embed(
        title=f"🚓 {safe_text(vehicle.name)} · {safe_text(vehicle.plate)}"[:256],
        color=discord.Color.blurple(),
    )
    embed.add_field(name="👤 Último conductor", value=driver_label(vehicle, drivers), inline=False)
    embed.add_field(
        name="🔧 Estado del vehículo",
        value=(
            f"├ ⛽ Combustible: **{metric(vehicle.fuel)}**\n"
            f"├ 🛢️ Aceite: **{metric(vehicle.oil)}**\n"
            f"└ ❤️ Salud: **{metric(vehicle.health)}**"
        ),
        inline=False,
    )
    embed.add_field(
        name="⚙️ Equipamiento",
        value=(
            f"├ 📷 ALPR: {flag_label(vehicle.alpr, 'Equipado', 'No equipado')}\n"
            f"└ 🚨 Sirena: {flag_label(vehicle.siren, 'Equipada', 'No equipada')}"
        ),
        inline=True,
    )
    embed.add_field(
        name="📋 Situación",
        value=(
            f"├ 🔒 {flag_label(vehicle.locked, 'Cerrado', 'Abierto')}\n"
            f"└ ⚖️ {flag_label(vehicle.impounded, 'Incautado', 'Sin incautación')}"
        ),
        inline=True,
    )
    if vehicle.visible_plate and vehicle.visible_plate != vehicle.plate:
        embed.add_field(name="Matrícula visible", value=safe_text(vehicle.visible_plate), inline=False)
    if vehicle.updated_at is not None:
        embed.add_field(name="Última actualización", value=discord.utils.format_dt(vehicle.updated_at, "R"))
    return embed


def fleet_page_count(rows: tuple[SarpFactionVehicle, ...], view: FleetView) -> int:
    count = len({row.name for row in rows}) if view is FleetView.SUMMARY else len(rows)
    return max(1, ceil(count / PAGE_SIZE))


def fleet_embed(
    snapshot: FleetSnapshot,
    rows: tuple[SarpFactionVehicle, ...],
    view: FleetView,
    *,
    model: str | None,
    alpr: AlprFilter,
    page: int,
    drivers: Mapping[str, int],
) -> discord.Embed:
    filters = safe_text(model) if model else "Todos los modelos"
    if alpr is not AlprFilter.ALL:
        filters += " · " + ("Con ALPR" if alpr is AlprFilter.EQUIPPED else "Sin ALPR")
    embed = discord.Embed(
        title=VIEW_LABELS[view],
        description=f"{filters}\n**{len(rows)} vehículos**",
        color=discord.Color.blurple(),
    )
    start = page * PAGE_SIZE
    if not rows:
        embed.description = f"{embed.description}\n\nNo hay vehículos con estos filtros."
    elif view is FleetView.SUMMARY:
        with_alpr = sum(v.alpr is True for v in rows)
        without_alpr = sum(v.alpr is False for v in rows)
        unknown_alpr = len(rows) - with_alpr - without_alpr
        alpr_text = f"{with_alpr} equipados · {without_alpr} sin equipar"
        if unknown_alpr:
            alpr_text += f" · {unknown_alpr} sin dato"
        embed.add_field(name="📷 ALPR", value=alpr_text, inline=False)
        impounded = sum(v.impounded is True for v in rows)
        unknown_impound = sum(v.impounded is None for v in rows)
        embed.add_field(
            name="⚖️ Incautados",
            value=(f"{impounded}" + (f" · {unknown_impound} sin dato" if unknown_impound else "")),
            inline=False,
        )
        models = sorted(Counter(v.name for v in rows).items(), key=lambda item: (-item[1], item[0]))
        model_lines = "\n".join(
            f"**{count}** · {safe_text(name)}" for name, count in models[start : start + PAGE_SIZE]
        )
        embed.description = f"{embed.description}\n\n**🚓 Por modelo**\n{model_lines}"
    else:
        lines = []
        for index, vehicle in enumerate(rows[start : start + PAGE_SIZE], start=start + 1):
            lines.append(
                f"**{index}. {safe_text(vehicle.plate)}** · {safe_text(vehicle.name[:80])}\n"
                f"　⛽ {metric(vehicle.fuel)}　🛢️ {metric(vehicle.oil)}　❤️ {metric(vehicle.health)}\n"
                f"　👤 {driver_label(vehicle, drivers)}"
            )
        embed.description = f"{embed.description}\n\n" + "\n\n".join(lines)
    embed.add_field(name="Consulta", value=discord.utils.format_dt(snapshot.fetched_at, "R"), inline=False)
    embed.set_footer(text=f"Página {page + 1}/{fleet_page_count(rows, view)}")
    return embed

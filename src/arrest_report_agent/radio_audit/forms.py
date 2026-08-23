"""Shared Discord form controls for radio queries and audits."""

from __future__ import annotations

from enum import StrEnum

import discord

from arrest_report_agent.application.date_ranges import DateRangePeriod


class RadioResultVisibility(StrEnum):
    PUBLIC = "public"
    PRIVATE = "private"


def build_period_select(
    custom_id: str,
    *,
    allowed_days: tuple[int, ...],
    default_days: int = 1,
) -> discord.ui.StringSelect:  # type: ignore[type-var]
    labels = {
        1: "Último día",
        2: "Últimos 2 días",
        7: "Últimos 7 días",
        14: "Últimos 14 días",
        30: "Últimos 30 días",
    }
    options = [
        discord.SelectOption(
            label=labels[days],
            value=str(days),
            default=days == default_days,
        )
        for days in allowed_days
    ]
    options.append(discord.SelectOption(label="Rango personalizado", value="custom"))
    return discord.ui.StringSelect(
        custom_id=custom_id,
        options=options,
        min_values=1,
        max_values=1,
    )


def build_visibility_select(custom_id: str) -> discord.ui.StringSelect:  # type: ignore[type-var]
    return discord.ui.StringSelect(
        custom_id=custom_id,
        options=[
            discord.SelectOption(
                label="Visible para todos",
                value=RadioResultVisibility.PUBLIC.value,
                emoji="🌐",
                default=True,
            ),
            discord.SelectOption(
                label="Solo para mí",
                value=RadioResultVisibility.PRIVATE.value,
                emoji="🔒",
            ),
        ],
        min_values=1,
        max_values=1,
    )


def selected_period(select: discord.ui.StringSelect) -> DateRangePeriod:  # type: ignore[type-var]
    return DateRangePeriod(next(iter(select.values or ()), DateRangePeriod.LAST_1_DAY.value))


def selected_visibility(select: discord.ui.StringSelect) -> RadioResultVisibility:  # type: ignore[type-var]
    return RadioResultVisibility(next(iter(select.values or ()), RadioResultVisibility.PUBLIC.value))

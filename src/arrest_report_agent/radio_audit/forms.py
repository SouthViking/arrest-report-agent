"""Shared Discord form controls for radio queries and audits."""

from __future__ import annotations

from enum import StrEnum

import discord

from arrest_report_agent.application.date_ranges import DateTimeRangePeriod


class RadioResultVisibility(StrEnum):
    PUBLIC = "public"
    PRIVATE = "private"


def build_interval_select(
    custom_id: str,
    *,
    allowed_periods: tuple[DateTimeRangePeriod, ...],
    default: DateTimeRangePeriod = DateTimeRangePeriod.LAST_1_DAY,
) -> discord.ui.StringSelect:  # type: ignore[type-var]
    labels = {
        DateTimeRangePeriod.LAST_1_HOUR: "Última hora",
        DateTimeRangePeriod.LAST_2_HOURS: "Últimas 2 horas",
        DateTimeRangePeriod.LAST_4_HOURS: "Últimas 4 horas",
        DateTimeRangePeriod.LAST_8_HOURS: "Últimas 8 horas",
        DateTimeRangePeriod.LAST_1_DAY: "Último día",
        DateTimeRangePeriod.LAST_2_DAYS: "Últimos 2 días",
        DateTimeRangePeriod.LAST_7_DAYS: "Últimos 7 días",
        DateTimeRangePeriod.LAST_14_DAYS: "Últimos 14 días",
        DateTimeRangePeriod.LAST_30_DAYS: "Últimos 30 días",
    }
    options = [
        discord.SelectOption(
            label=labels[period],
            value=period.value,
            default=period is default,
        )
        for period in allowed_periods
    ]
    options.append(
        discord.SelectOption(
            label="Rango personalizado",
            value=DateTimeRangePeriod.CUSTOM.value,
        )
    )
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


def selected_interval(select: discord.ui.StringSelect) -> DateTimeRangePeriod:  # type: ignore[type-var]
    return DateTimeRangePeriod(next(iter(select.values or ()), DateTimeRangePeriod.LAST_1_DAY.value))


def selected_visibility(select: discord.ui.StringSelect) -> RadioResultVisibility:  # type: ignore[type-var]
    return RadioResultVisibility(next(iter(select.values or ()), RadioResultVisibility.PUBLIC.value))

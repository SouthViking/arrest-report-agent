"""Reusable Discord controls shared by deterministic date-range commands."""

from __future__ import annotations

from collections.abc import Iterable

import discord

from arrest_report_agent.application.date_ranges import DateRangePeriod

DATE_PERIOD_LABELS: dict[DateRangePeriod, str] = {
    DateRangePeriod.LAST_1_DAY: "Último día",
    DateRangePeriod.LAST_2_DAYS: "Últimos 2 días",
    DateRangePeriod.LAST_7_DAYS: "Últimos 7 días",
    DateRangePeriod.LAST_14_DAYS: "Últimos 14 días",
    DateRangePeriod.LAST_30_DAYS: "Últimos 30 días",
    DateRangePeriod.LAST_90_DAYS: "Últimos 90 días",
    DateRangePeriod.CUSTOM: "Rango personalizado",
}


def build_date_period_select(
    custom_id: str,
    *,
    periods: Iterable[DateRangePeriod],
    default: DateRangePeriod,
) -> discord.ui.StringSelect:  # type: ignore[type-var]
    """Build one date preset selector with a single selected default."""

    options = [
        discord.SelectOption(
            label=DATE_PERIOD_LABELS[period],
            value=period.value,
            default=period is default,
        )
        for period in periods
    ]
    return discord.ui.StringSelect(
        custom_id=custom_id,
        options=options,
        min_values=1,
        max_values=1,
    )


def build_publication_checkbox(
    custom_id: str,
    *,
    default: bool,
) -> discord.ui.Checkbox:
    """Build the common public/private result toggle."""

    return discord.ui.Checkbox(custom_id=custom_id, default=default)


def selected_date_period(
    select: discord.ui.StringSelect,  # type: ignore[type-var]
    *,
    default: DateRangePeriod,
) -> DateRangePeriod:
    return DateRangePeriod(next(iter(select.values or ()), default.value))

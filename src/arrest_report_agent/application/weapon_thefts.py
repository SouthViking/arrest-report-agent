"""Shared cache and failure boundaries for serial queries initiated by evidence or by the agent."""

import logging

from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.domain.weapon_serials import normalize_serial

from .ports import WeaponTheftLookup, WeaponTheftSerialResult
from .service_context import ReviewRunContext

LOGGER = logging.getLogger(__name__)
MAX_SERIALS_PER_CALL = 50
MAX_LOOKUP_BATCHES = 5


def _key(serial: str) -> str:
    try:
        return normalize_serial(serial)
    except ValueError:
        return serial


async def lookup_weapon_thefts(
    context: ReviewRunContext,
    lookup: WeaponTheftLookup,
    serials: tuple[str, ...],
    *,
    report_channel_ids: frozenset[int],
) -> tuple[WeaponTheftSerialResult, ...]:
    keys = tuple(dict.fromkeys(_key(serial) for serial in serials))
    missing = tuple(key for key in keys if key not in context.weapon_theft_results)
    if missing and context.weapon_theft_lookup_batches < MAX_LOOKUP_BATCHES:
        context.weapon_theft_lookup_batches += 1
        try:
            found = await lookup.lookup(context.case.guild_id, missing)
            for result in found:
                if _key(result.serial) in missing:
                    context.weapon_theft_results[_key(result.serial)] = result
        except Exception as error:
            log_event(
                LOGGER,
                "warning",
                "review_weapon_theft_lookup_failed",
                guild_id=context.case.guild_id,
                **exception_fields(error),
            )
    results = tuple(
        context.weapon_theft_results.get(key, WeaponTheftSerialResult(key, "unavailable")) for key in keys
    )
    for result in results:
        if result.status == "unavailable":
            context.weapon_theft_results.pop(_key(result.serial), None)
    case = context.case
    is_report = (
        case.structured_report_id is not None
        or case.structured_submission_id is not None
        or (case.thread_id is not None and case.parent_channel_id in report_channel_ids)
    )
    active = {result.serial for result in results if result.status == "reported_stolen"}
    unseen = active - context.weapon_theft_notified_serials
    bags = {
        bag: tuple(serial for serial in values if serial in unseen)
        for bag, values in context.weapon_serials_by_bag.items()
    }
    bags = {bag: values for bag, values in bags.items() if values}
    if is_report and bags:
        try:
            await lookup.notify(case, bags)
            context.weapon_theft_notified_serials.update(unseen)
        except Exception as error:
            log_event(
                LOGGER,
                "warning",
                "review_weapon_theft_notification_failed",
                guild_id=case.guild_id,
                **exception_fields(error),
            )
    return results


def theft_informational_notes(context: ReviewRunContext) -> list[str]:
    return [
        f"El arma con serial {serial}, identificada en la bolsa {bag}, tiene un reporte de robo abierto."
        for bag, serials in context.weapon_serials_by_bag.items()
        for serial in serials
        if (result := context.weapon_theft_results.get(serial)) is not None
        and result.status == "reported_stolen"
    ][:50]

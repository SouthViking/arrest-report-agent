"""Fixed IANA timezone choices shared by user-facing local-time forms."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

TIMEZONE_OPTIONS = (
    ("🇦🇷", "America/Argentina/Buenos_Aires"),
    ("🇨🇱", "America/Santiago"),
    ("🇨🇴", "America/Bogota"),
    ("🇻🇪", "America/Caracas"),
    ("🇪🇸", "Europe/Madrid"),
    ("🇪🇸", "Atlantic/Canary"),
    ("🇲🇽", "America/Mexico_City"),
    ("🇲🇽", "America/Mazatlan"),
    ("🇲🇽", "America/Hermosillo"),
    ("🇲🇽", "America/Cancun"),
    ("🇲🇽", "America/Tijuana"),
)
TIMEZONE_KEYS = frozenset(key for _, key in TIMEZONE_OPTIONS)


def timezone_label(key: str) -> str:
    return next(f"{flag} {zone}" for flag, zone in TIMEZONE_OPTIONS if zone == key)


def parse_local_datetime(raw: str, timezone_key: str) -> datetime:
    """Parse a local minute without silently resolving DST gaps or ambiguities."""

    if timezone_key not in TIMEZONE_KEYS:
        raise ValueError("Selecciona una zona horaria de la lista.")
    try:
        local = datetime.strptime(raw.strip(), "%d/%m/%Y %H:%M")
    except ValueError as error:
        raise ValueError("Usa el formato DD/MM/AAAA HH:MM en ambas fechas.") from error
    zone = ZoneInfo(timezone_key)
    candidate = local.replace(tzinfo=zone)
    utc = candidate.astimezone(UTC)
    if utc.astimezone(zone).replace(tzinfo=None) != local:
        raise ValueError("Esa hora no existe por el cambio de horario. Elige otra hora.")
    if candidate.utcoffset() != candidate.replace(fold=1).utcoffset():
        raise ValueError("Esa hora se repite por el cambio de horario. Elige otra hora.")
    return utc

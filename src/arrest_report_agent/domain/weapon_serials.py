"""Canonical serials and conservative extraction from explicit weapon analyses."""

import re
import unicodedata

_WEAPON = re.compile(
    r"\b(?:bal[ií]stica|ballistics|arma|pistola|rev[oó]lver|escopeta|rifle|fusil|"
    r"subfusil|carabina|ametralladora)\b",
    re.IGNORECASE,
)
_SERIAL = re.compile(
    r"\b(?:serial|n[uú]mero\s+de\s+serie|nro\.?\s+de\s+serie)\s*[:#]\s*"
    r"([^,;\n\r]+)",
    re.IGNORECASE,
)
_MISSING = frozenset(
    {
        "sin serial",
        "no definido",
        "no tiene",
        "ilegible",
        "borrado",
        "ninguno",
        "null",
        "none",
        "n/a",
        "desconocido",
        "no visible",
        "no disponible",
    }
)


def normalize_serial(value: str) -> str:
    serial = " ".join(unicodedata.normalize("NFKC", value).split()).upper()
    if not re.fullmatch(r"[A-Z0-9][A-Z0-9 ._/-]{0,99}", serial):
        raise ValueError(
            "El serial debe tener entre 1 y 100 caracteres: letras, números, espacios o . _ / -."
        )
    return serial


def extract_weapon_serials(result: str) -> tuple[str, ...]:
    """Require both weapon context and an explicit serial label; never infer from sample IDs."""
    if not _WEAPON.search(result):
        return ()
    serials: list[str] = []
    for match in _SERIAL.finditer(result):
        raw = match.group(1).strip().rstrip(".")
        if raw.casefold() in _MISSING:
            continue
        try:
            serial = normalize_serial(raw)
        except ValueError:
            continue
        if serial not in serials:
            serials.append(serial)
    return tuple(serials)

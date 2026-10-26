"""Trusted, attributable staff approvals, separate from model-supplied review data."""

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

from arrest_report_agent.domain import FindingSeverity, ReviewFinding, ReviewIssueType


@dataclass(frozen=True)
class StaffApproval:
    user_id: int
    source_id: int
    reason: str
    source: Literal["message", "command"] = "message"
    instruction_message_id: int | None = None

    def finding(self, *, guild_id: int, channel_id: int, detail: str) -> ReviewFinding:
        evidence = (
            f"https://discord.com/channels/{guild_id}/{channel_id}/{self.source_id}"
            if self.source == "message"
            else f"/aprobar-arresto · interacción {self.source_id}"
        )
        if self.instruction_message_id is not None:
            evidence += (
                f" · Orden previa: https://discord.com/channels/"
                f"{guild_id}/{channel_id}/{self.instruction_message_id}"
            )
        return ReviewFinding(
            issue_type=ReviewIssueType.OTHER,
            severity=FindingSeverity.OBSERVATION,
            title="Aprobación excepcional de Command/Executive Staff",
            detail=f"Autorizado por <@{self.user_id}>. {detail} Motivo: {self.reason[:500]}"[:700],
            recommendation="Conserva esta autorización como constancia de la aprobación excepcional.",
            evidence=evidence,
        )


def _normalized_text(content: str) -> str:
    text = "".join(
        c for c in unicodedata.normalize("NFKD", content.casefold()) if not unicodedata.combining(c)
    )
    return " ".join(re.sub(r"<@!?\d+>", " ", text).split())


def _ambiguous(text: str) -> bool:
    return (
        any(token in text for token in ("?", "¿", '"', "'", "`", ">", "«", "»"))
        or re.search(
            r"\b(no|nunca|jamas|falso|mentira|tampoco|si|cuando|despues|luego|antes|salvo|excepto|pero|hasta|tras|"
            r"dijo|dice|ejemplo|prueba|test|en cuanto|una vez|a menos)\b",
            text,
        )
        is not None
    )


def is_explicit_approval(content: str) -> bool:
    """Recognize direct imperatives and explicit natural-language approval orders.

    Staff can always use /aprobar-arresto when their wording is outside this small grammar.
    Role and message provenance are checked separately against Discord, never against this text.
    """

    text = _normalized_text(content)
    text = re.sub(r"^por favor[, :]*", "", text).strip()
    if _ambiguous(text):
        return False
    return (
        re.match(
            r"^(?:aprueba(?:lo| el reporte| este reporte)|"
            r"marca(?:lo| el reporte| este reporte) como aprobado|"
            r"dejalo aprobado|dale el (?:ok|visto bueno)|"
            r"autorizo (?:la aprobacion|aprobar(?:lo| el reporte| este reporte)))\b",
            text,
        )
        is not None
        or re.search(
            r"\b(?:te (?:estoy|estamos) dando|(?:te )?(?:doy|damos|reitero|reiteramos)) la orden\b"
            r".{0,180}\b(?:que lo dejes aprobado|que lo apruebes|de aprobar(?:lo| el reporte))\b",
            text,
        )
        is not None
        or re.search(
            r"\b(?:quiero|queremos|ordeno|ordenamos|autorizo|autorizamos) que "
            r"(?:lo apruebes|lo dejes aprobado|apruebes (?:el|este) reporte)\b",
            text,
        )
        is not None
    )


def is_approval_confirmation(content: str) -> bool:
    """A confirmation is insufficient on its own; Discord must verify the original instruction."""

    text = _normalized_text(content)
    if re.fullmatch(r"(?:ahora si|si|confirmo|ratifico|confirmado|adelante)[.!]*", text):
        return True
    return (
        not _ambiguous(text)
        and re.fullmatch(
            r"(?:(?:mira|revisa) bien tus instrucciones[,;: ]+)?"
            r"(?:soy (?:de )?(?:command|executive)(?: staff)?[,;: ]+)?es una orden[.!]*",
            text,
        )
        is not None
    )


def invalidates_approval_context(content: str) -> bool:
    """Do not carry an earlier approval past a revocation, condition or a different review request."""

    text = _normalized_text(content)
    return (
        re.fullmatch(r"(?:no|todavia no|ya no|nunca|jamas)[.!]*", text) is not None
        or re.search(
            r"\b(?:aprob\w*|aprueb\w*|revis\w*|cancela\w*|espera\w*|pendiente|"
            r"no lo hagas|olvidalo|visto bueno|ok)\b",
            text,
        )
        is not None
    )

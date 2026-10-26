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

    def finding(self, *, guild_id: int, channel_id: int, detail: str) -> ReviewFinding:
        evidence = (
            f"https://discord.com/channels/{guild_id}/{channel_id}/{self.source_id}"
            if self.source == "message"
            else f"/aprobar-arresto · interacción {self.source_id}"
        )
        return ReviewFinding(
            issue_type=ReviewIssueType.OTHER,
            severity=FindingSeverity.OBSERVATION,
            title="Aprobación excepcional de Command/Executive Staff",
            detail=f"Autorizado por <@{self.user_id}>. {detail} Motivo: {self.reason[:500]}"[:700],
            recommendation="Conserva esta autorización como constancia de la aprobación excepcional.",
            evidence=evidence,
        )


def is_explicit_approval(content: str) -> bool:
    """Accept clear direct imperatives; ambiguous, quoted or conditional requests fail closed.

    Staff can always use /aprobar-arresto when their wording is outside this small grammar.
    Role and message provenance are checked separately against Discord, never against this text.
    """

    text = "".join(
        c for c in unicodedata.normalize("NFKD", content.casefold()) if not unicodedata.combining(c)
    ).strip()
    text = re.sub(r"^(?:<@!?\d+>\s*)+", "", text)
    text = re.sub(r"^por favor[, :]*", "", text).strip()
    if any(token in text for token in ("?", "¿", "\n", '"', "'", "`", ">")):
        return False
    if re.search(
        r"\b(no|nunca|tampoco|si|cuando|despues|luego|antes|salvo|excepto|pero|hasta|tras|"
        r"dijo|dice|ejemplo|prueba|test|en cuanto|una vez|a menos)\b", text,
    ):
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
    )

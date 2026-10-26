"""Enforce warrant clearance independently of the agent's proposed verdict."""

from arrest_report_agent.application.approvals import StaffApproval
from arrest_report_agent.domain import (
    FindingSeverity,
    ReviewFinding,
    ReviewIssueType,
    ReviewPublication,
    ReviewScope,
    ReviewVerdict,
)

from .lookup import ArrestWarrantCheck

PENDING_REASON = "La aprobación queda pendiente de verificar las órdenes de arresto abiertas en SARP."


def apply_warrant_policy(
    publication: ReviewPublication,
    check: ArrestWarrantCheck,
    *,
    approval: StaffApproval | None = None,
    guild_id: int = 0,
    channel_id: int = 0,
) -> ReviewPublication:
    if publication.review_scope is not ReviewScope.FULL:
        return publication
    # A model-supplied historical finding cannot override the fresh authoritative check.
    findings = [f for f in publication.findings if f.issue_type is not ReviewIssueType.OPEN_ARREST_WARRANT]
    changes: dict[str, object] = {"findings": findings}
    if (
        approval is not None
        and check.status == "confirmed"
        and check.warrants
        and publication.verdict is ReviewVerdict.APPROVED
        and not any(f.severity is FindingSeverity.REQUIRED for f in findings)
    ):
        ids = ", ".join(f"#{w.warrant_id}" for w in check.warrants)
        finding = approval.finding(
            guild_id=guild_id,
            channel_id=channel_id,
            detail=f"SARP mantiene órdenes abiertas: {ids}. Esta autorización no cierra las órdenes.",
        )
        return publication.model_copy(update={"findings": [finding, *findings[:11]]})
    if check.warrants:
        ids = ", ".join(f"#{w.warrant_id}" for w in check.warrants)
        finding = ReviewFinding(
            severity=FindingSeverity.REQUIRED,
            issue_type=ReviewIssueType.OPEN_ARREST_WARRANT,
            title="Orden de arresto abierta",
            detail=(
                f"SARP confirma órdenes de arresto abiertas: {ids}. "
                "Impiden aprobar el reporte, aunque estén pendientes de firma."
            ),
            recommendation="Gestiona el cierre de las órdenes y solicita una nueva revisión.",
            evidence=f"Consulta SARP: {check.checked_at.isoformat()}",
        )
        # Preserve required report corrections ahead of optional observations within the schema limit.
        findings.sort(key=lambda f: f.severity is not FindingSeverity.REQUIRED)
        changes.update(
            verdict=ReviewVerdict.CORRECTIONS_REQUIRED,
            summary="El reporte requiere correcciones: hay órdenes de arresto abiertas en SARP.",
            findings=[finding, *findings[:11]],
        )
        omitted = [f.title for f in findings[11:] if f.severity is FindingSeverity.REQUIRED]
        if omitted:
            changes["manual_review_reason"] = (
                f"{publication.manual_review_reason or ''} Correcciones adicionales pendientes: "
                + "; ".join(omitted)
            ).strip()[:500]
    elif check.approval_check == "pending":
        changes["manual_review_reason"] = (
            f"{PENDING_REASON} {publication.manual_review_reason or ''}"
        ).strip()[:500]
        if any(f.severity is FindingSeverity.REQUIRED for f in findings):
            changes.update(
                verdict=ReviewVerdict.CORRECTIONS_REQUIRED,
                summary="El reporte requiere correcciones; la consulta de órdenes en SARP sigue pendiente.",
            )
        else:
            changes.update(
                verdict=ReviewVerdict.INCONCLUSIVE,
                summary="La aprobación queda pendiente de verificar las órdenes de arresto en SARP.",
            )
    elif publication.verdict is ReviewVerdict.CORRECTIONS_REQUIRED and not any(
        f.severity is FindingSeverity.REQUIRED for f in findings
    ):
        # Clearance removes this blocker; it does not fabricate an assessment of the rest of the report.
        changes.update(
            verdict=ReviewVerdict.INCONCLUSIVE,
            summary="SARP no registra órdenes abiertas; falta completar la evaluación del reporte.",
            manual_review_reason="Vuelve a evaluar el reporte con el estado actual de las órdenes.",
        )
    return ReviewPublication.model_validate({**publication.model_dump(), **changes})

"""Shared serialization for SARP arrest-report details."""

from __future__ import annotations

from arrest_report_agent.application.ports import ArrestReportDetail


def arrest_detail_payload(
    report: ArrestReportDetail,
    *,
    bound: bool,
) -> dict[str, object]:
    """Return the canonical compact representation exposed to the agent."""

    return {
        "ok": True,
        "bound_to_current_thread": bound,
        "report": {
            "report_id": report.report_id,
            "suspect_name": report.suspect_name,
            "officer_name": report.officer_name,
            "description": report.description,
            "created_at": report.created_at.isoformat(),
            "updated_at": (
                report.updated_at.isoformat() if report.updated_at is not None else None
            ),
            "charges": [
                {
                    "charge_id": charge.charge_id,
                    "code": charge.code,
                    "name": charge.name,
                    "type": charge.charge_type,
                    "hours": charge.hours,
                    "fine": charge.fine,
                }
                for charge in report.charges
            ],
        },
    }

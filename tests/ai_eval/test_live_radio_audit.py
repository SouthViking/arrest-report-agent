from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from arrest_report_agent.knowledge import RadioCodeCatalog
from arrest_report_agent.radio_audit import RadioAuditAgent, RadioManualCatalog
from arrest_report_agent.radio_audit.models import ParsedRadioLog, RadioLogQueryResult

pytestmark = [pytest.mark.ai_eval]


def _record(identifier: int, member: str, body: str, minute: int) -> ParsedRadioLog:
    return ParsedRadioLog(
        id=identifier,
        faction_id=3,
        character_id=100 + identifier,
        character_name=member,
        created_at=datetime(2026, 8, 22, 20, tzinfo=UTC) + timedelta(minutes=minute),
        raw_message=f"[CH: SCC, S: 1] {member}: {body}",
        channel="SCC",
        slot=1,
        speaker=member,
        body=body,
    )


def _sample(*records: ParsedRadioLog) -> RadioLogQueryResult:
    return RadioLogQueryResult(
        faction_id=3,
        date_from="2026-08-22",
        date_to="2026-08-22",
        slot=1,
        raw_total=len(records),
        scanned_count=len(records),
        malformed_count=0,
        items=records,
    )


async def _audit(sample, settings, model, persona):
    agent = RadioAuditAgent(  # type: ignore[arg-type]
        settings,
        model,
        persona,
        RadioCodeCatalog.load_default(),
        RadioManualCatalog.load_default(),
    )
    return await agent.audit(sample, guild_id=1, requester_id=2, interaction_id=3)


async def test_live_radio_audit_accepts_clear_contextual_exchange(settings, model, persona) -> None:
    result = await _audit(
        _sample(
            _record(1, "Terry Davis", "Central, Adam-21 en 10-8 desde Ganton.", 0),
            _record(2, "Terry Davis", "Central, Adam-21 atendiendo llamado 154.", 1),
            _record(3, "Terry Davis", "Central, Adam-21 10-97 en llamado 154.", 5),
            _record(4, "Terry Davis", "Central, Adam-21 situación controlada.", 12),
        ),
        settings,
        model,
        persona,
    )

    assert result.total_radios == 4
    assert result.severe_count == 0


async def test_live_radio_audit_detects_grounded_operational_issue(settings, model, persona) -> None:
    result = await _audit(
        _sample(
            _record(10, "Derek Valdez", "Estoy persiguiendo un auto.", 0),
            _record(11, "Derek Valdez", "Lo perdí, da igual.", 1),
        ),
        settings,
        model,
        persona,
    )

    assert result.moderate_count + result.severe_count >= 1
    assert all(finding.manual_reference for member in result.members for finding in member.findings)


async def test_live_radio_audit_groups_related_messages_by_member(settings, model, persona) -> None:
    result = await _audit(
        _sample(
            _record(20, "Andy Quintero", "Central, Ocean-12 en persecución.", 0),
            _record(21, "Andy Quintero", "Vehículo Sultan negro, dirección norte.", 1),
            _record(22, "Andy Quintero", "Perdí visual.", 3),
        ),
        settings,
        model,
        persona,
    )

    assert len(result.members) <= 1
    assert all(
        set(finding.radio_ids).issubset({20, 21, 22})
        for member in result.members
        for finding in member.findings
    )

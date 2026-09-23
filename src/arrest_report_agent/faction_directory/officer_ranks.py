"""Conservative, faction-bound lookup of a report officer's current SARP rank."""

from __future__ import annotations

import logging
from difflib import SequenceMatcher
from typing import Literal

from arrest_report_agent.application.member_names import normalize_person_name
from arrest_report_agent.application.ports import (
    FactionOfficerRankLookupResult,
    FactionOfficerRankStatus,
)
from arrest_report_agent.config.logging import exception_fields, log_event
from arrest_report_agent.infrastructure.sarp.failures import classify_sarp_failure

from .models import FactionDirectoryMember, FactionDirectorySnapshot
from .service import UNKNOWN_RANK, FactionDirectoryService

LOGGER = logging.getLogger(__name__)
MIN_FUZZY_SCORE = 0.90
MIN_FIRST_NAME_SCORE = 0.85
MIN_SURNAME_SCORE = 0.86
MIN_UNIQUE_MARGIN = 0.08
MAX_AMBIGUOUS_CANDIDATES = 3
type RankMatchMethod = Literal["normalized_exact", "compact_exact", "fuzzy"]


class SarpFactionOfficerRankLookup:
    """Reuse the shared roster cache without exposing its complete membership to the agent."""

    def __init__(self, directory: FactionDirectoryService, *, faction_id: int) -> None:
        if faction_id <= 0:
            raise ValueError("faction_id must be positive")
        self.directory = directory
        self.faction_id = faction_id

    async def find(self, officer_name: str, *, fuzzy: bool = True) -> FactionOfficerRankLookupResult:
        query = normalize_person_name(officer_name)
        if len(query.split()) < 2:
            return FactionOfficerRankLookupResult(status="invalid_name", officer_name=officer_name)
        try:
            snapshot = await self.directory.snapshot(self.faction_id)
        except Exception as error:
            code, retry_after = classify_sarp_failure(error)
            log_event(
                LOGGER,
                "warning",
                "agent_report_officer_rank_lookup_failed",
                faction_id=self.faction_id,
                failure=code,
                exc_info=error,
                **exception_fields(error),
            )
            return FactionOfficerRankLookupResult(
                status="unavailable",
                officer_name=officer_name,
                error=code,
                retry_after_seconds=retry_after,
            )
        return _resolve_rank(snapshot, officer_name, query, fuzzy=fuzzy)


def _resolve_rank(
    snapshot: FactionDirectorySnapshot,
    officer_name: str,
    query: str,
    *,
    fuzzy: bool,
) -> FactionOfficerRankLookupResult:
    exact = [member for member in snapshot.members if normalize_person_name(member.name) == query]
    if exact:
        return _exact_result(officer_name, exact, method="normalized_exact")

    compact_query = query.replace(" ", "")
    compact = [
        member
        for member in snapshot.members
        if normalize_person_name(member.name).replace(" ", "") == compact_query
    ]
    if compact:
        return _exact_result(officer_name, compact, method="compact_exact")
    if not fuzzy:
        return FactionOfficerRankLookupResult(status="not_found", officer_name=officer_name)

    ranked = sorted(
        (
            (score, member)
            for member in snapshot.members
            if (score := _fuzzy_score(query, normalize_person_name(member.name))) is not None
        ),
        key=lambda item: (-item[0], item[1].name.casefold()),
    )
    if not ranked:
        return FactionOfficerRankLookupResult(status="not_found", officer_name=officer_name)
    if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < MIN_UNIQUE_MARGIN:
        return FactionOfficerRankLookupResult(
            status="ambiguous",
            officer_name=officer_name,
            candidates=tuple(member.name for _, member in ranked[:MAX_AMBIGUOUS_CANDIDATES]),
        )
    return _member_result(officer_name, ranked[0][1], method="fuzzy")


def _exact_result(
    officer_name: str,
    members: list[FactionDirectoryMember],
    *,
    method: RankMatchMethod,
) -> FactionOfficerRankLookupResult:
    if len(members) > 1:
        return FactionOfficerRankLookupResult(
            status="ambiguous",
            officer_name=officer_name,
            candidates=tuple(member.name for member in members[:MAX_AMBIGUOUS_CANDIDATES]),
        )
    return _member_result(officer_name, members[0], method=method)


def _member_result(
    officer_name: str,
    member: FactionDirectoryMember,
    *,
    method: RankMatchMethod,
) -> FactionOfficerRankLookupResult:
    if member.rank_name == UNKNOWN_RANK:
        status: FactionOfficerRankStatus = "rank_unknown"
    else:
        status = "possible_match" if method == "fuzzy" else "matched"
    return FactionOfficerRankLookupResult(
        status=status,
        officer_name=officer_name,
        member_name=member.name,
        current_rank=None if member.rank_name == UNKNOWN_RANK else member.rank_name,
        match_method=method,
    )


def _fuzzy_score(query: str, candidate: str) -> float | None:
    query_parts = query.split()
    candidate_parts = candidate.split()
    if len(query_parts) < 2 or len(candidate_parts) < 2:
        return None
    query_first, candidate_first = query_parts[0], candidate_parts[0]
    if (
        len(query_first) < 3
        or len(candidate_first) < 3
        or query_first[0] != candidate_first[0]
        or SequenceMatcher(None, query_first, candidate_first).ratio() < MIN_FIRST_NAME_SCORE
    ):
        return None
    query_surname = "".join(query_parts[1:])
    candidate_surname = "".join(candidate_parts[1:])
    if (
        len(query_surname) < 4
        or len(candidate_surname) < 4
        or SequenceMatcher(None, query_surname, candidate_surname).ratio() < MIN_SURNAME_SCORE
    ):
        return None
    score = SequenceMatcher(None, query.replace(" ", ""), candidate.replace(" ", "")).ratio()
    return score if score >= MIN_FUZZY_SCORE else None

from __future__ import annotations

import pytest

from arrest_report_agent.faction_directory import officer_ranks
from arrest_report_agent.faction_directory.models import (
    FactionDirectoryMember,
    FactionDirectorySnapshot,
)
from arrest_report_agent.faction_directory.officer_ranks import SarpFactionOfficerRankLookup
from arrest_report_agent.faction_directory.service import UNKNOWN_RANK


def _member(name: str, rank: str = "Sergeant") -> FactionDirectoryMember:
    return FactionDirectoryMember(
        name=name,
        online=False,
        on_duty=False,
        rank_name=rank,
        rank_order=3,
        salary=None,
        last_connected_at=None,
        faction_time_minutes=0,
    )


class Directory:
    def __init__(self, *members: FactionDirectoryMember, error: Exception | None = None) -> None:
        self.members = members
        self.error = error
        self.calls: list[int] = []

    async def snapshot(self, faction_id: int) -> FactionDirectorySnapshot:
        self.calls.append(faction_id)
        if self.error is not None:
            raise self.error
        return FactionDirectorySnapshot(faction_id=faction_id, members=self.members)


@pytest.mark.asyncio
async def test_rank_lookup_matches_accents_spaces_and_underscores() -> None:
    directory = Directory(_member("José De La Rosa", "Deputy Sheriff"))
    lookup = SarpFactionOfficerRankLookup(directory, faction_id=3)  # type: ignore[arg-type]

    exact, compact = await lookup.find_many(("JOSE_DE_LA_ROSA", "Jose DelaRosa"))

    assert (exact.status, exact.match_method, exact.current_rank) == (
        "matched",
        "normalized_exact",
        "Deputy Sheriff",
    )
    assert (compact.status, compact.match_method) == ("matched", "compact_exact")
    assert directory.calls == [3]


@pytest.mark.asyncio
async def test_rank_lookup_refreshes_its_snapshot_after_two_minutes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [100.0]
    monkeypatch.setattr(officer_ranks, "monotonic", lambda: clock[0])
    directory = Directory(_member("Charles Harrington", "Sergeant"))
    lookup = SarpFactionOfficerRankLookup(directory, faction_id=3)  # type: ignore[arg-type]

    assert (await lookup.find("Charles Harrington")).current_rank == "Sergeant"
    directory.members = (_member("Charles Harrington", "Lieutenant"),)
    clock[0] += 119
    assert (await lookup.find("Charles Harrington")).current_rank == "Sergeant"
    clock[0] += 2
    assert (await lookup.find("Charles Harrington")).current_rank == "Lieutenant"
    assert directory.calls == [3, 3]


@pytest.mark.asyncio
async def test_rank_lookup_uses_conservative_fuzzy_and_exposes_ambiguity() -> None:
    lookup = SarpFactionOfficerRankLookup(  # type: ignore[arg-type]
        Directory(_member("Charles Harrington")), faction_id=3
    )
    possible = await lookup.find("Charles Harington")
    strict = await lookup.find("Charles Harington", fuzzy=False)
    assert (possible.status, possible.match_method, possible.current_rank) == (
        "possible_match",
        "fuzzy",
        "Sergeant",
    )
    assert strict.status == "not_found"

    ambiguous = SarpFactionOfficerRankLookup(  # type: ignore[arg-type]
        Directory(_member("Charles Harrington"), _member("Charles Harington")), faction_id=3
    )
    result = await ambiguous.find("Charles Haringto")
    assert result.status == "ambiguous"
    assert len(result.candidates) == 2
    assert result.current_rank is None


@pytest.mark.asyncio
async def test_rank_lookup_does_not_invent_rank_or_identity() -> None:
    lookup = SarpFactionOfficerRankLookup(  # type: ignore[arg-type]
        Directory(_member("Charles Harrington", UNKNOWN_RANK)), faction_id=3
    )
    assert (await lookup.find("Charles Harrington")).status == "rank_unknown"
    assert (await lookup.find("C. Harrington")).status == "not_found"
    assert (await lookup.find("Harrington")).status == "invalid_name"


@pytest.mark.asyncio
async def test_rank_lookup_reports_unavailable_separately_from_not_found() -> None:
    directory = Directory(error=RuntimeError("offline"))
    lookup = SarpFactionOfficerRankLookup(  # type: ignore[arg-type]
        directory, faction_id=3
    )
    results = await lookup.find_many(("Charles Harrington", "Marco Ruiz", "Harrington"))
    assert [result.status for result in results] == ["unavailable", "unavailable", "invalid_name"]
    assert all(result.error is not None for result in results[:2])
    assert directory.calls == [3]

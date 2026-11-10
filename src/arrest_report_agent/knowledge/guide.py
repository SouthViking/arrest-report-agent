"""Versioned report-writing guidance exposed selectively to the agent."""

from __future__ import annotations

import json
from collections.abc import Sequence
from enum import StrEnum
from importlib.resources import files
from pathlib import Path
from typing import Any


class ReportGuideSection(StrEnum):
    OVERVIEW = "overview"
    WRITING_STYLE = "writing_style"
    NARRATIVE_STRUCTURE = "narrative_structure"
    ESSENTIAL_CONTENT = "essential_content"
    EVIDENCE = "evidence"
    PHYSICAL_DESCRIPTION = "physical_description"
    CHARGE_REVIEW = "charge_review"
    REVIEW_CALIBRATION = "review_calibration"


class ReportGuide:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.version = str(payload["version"])
        self.sections = dict(payload["sections"])

    @classmethod
    def load_default(cls) -> ReportGuide:
        resource = files("arrest_report_agent.knowledge").joinpath("data/report_guide.json")
        return cls.from_path(Path(str(resource)))

    @classmethod
    def from_path(cls, path: Path) -> ReportGuide:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(payload, dict) or not isinstance(payload.get("sections"), dict)
                or not payload["sections"]):
            raise ValueError("Report guide requires a non-empty sections object")
        return cls(payload)

    def get(self, sections: Sequence[str | ReportGuideSection] | None = None) -> dict[str, Any]:
        requested = tuple(str(section) for section in sections) if sections else ("overview",)
        unknown = [name for name in requested if name not in self.sections]
        selected = {name: self.sections[name] for name in requested if name in self.sections}
        return {
            "version": self.version,
            "available_sections": list(self.sections),
            "sections": selected,
            "unknown_sections": unknown,
        }

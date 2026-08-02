"""Versioned report-writing guidance exposed selectively to the agent."""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path
from typing import Any


class ReportGuide:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.version = str(payload["version"])
        self.sections = dict(payload["sections"])

    @classmethod
    def load_default(cls) -> ReportGuide:
        resource = files("arrest_report_agent.knowledge").joinpath("data/report_guide.json")
        payload = json.loads(Path(str(resource)).read_text(encoding="utf-8"))
        return cls(payload)

    def get(self, sections: list[str] | None = None) -> dict[str, Any]:
        requested = sections or ["overview"]
        unknown = [name for name in requested if name not in self.sections]
        selected = {name: self.sections[name] for name in requested if name in self.sections}
        return {
            "version": self.version,
            "available_sections": list(self.sections),
            "sections": selected,
            "unknown_sections": unknown,
        }


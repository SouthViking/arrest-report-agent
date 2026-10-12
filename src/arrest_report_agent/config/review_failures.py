"""Additive failure copy for older database personas without replacing custom text."""

from __future__ import annotations

import json
from functools import lru_cache
from importlib.resources import files

from .persona import BotPersona, PersonaReviewRecoveryFailureMessages


def review_recovery_failure_messages(persona: BotPersona) -> PersonaReviewRecoveryFailureMessages:
    configured = persona.messages.failures.review_recovery_exhausted
    return configured if configured is not None else _packaged_recovery_messages(persona.id)


@lru_cache(maxsize=32)
def _packaged_recovery_messages(persona_id: str) -> PersonaReviewRecoveryFailureMessages:
    resource = files("arrest_report_agent").joinpath("config", "personas", f"{persona_id}.json")
    try:
        payload = json.loads(resource.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return PersonaReviewRecoveryFailureMessages()
    persona = BotPersona.model_validate(payload)
    return persona.messages.failures.review_recovery_exhausted or PersonaReviewRecoveryFailureMessages()

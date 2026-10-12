from __future__ import annotations

from uuid import uuid4

import pytest

from arrest_report_agent.config import (
    AccessPolicyKey,
    AccessPolicyMode,
    AccessPolicySnapshot,
    BotPersona,
    GuildAgentConfig,
    GuildConfigSnapshot,
    GuildFeatureSnapshot,
    GuildRuntimeSettings,
    PackagedPersonaProvider,
    ReviewFailureNotificationOptions,
)
from arrest_report_agent.config.database import POLICY_SUBJECT_TYPES
from arrest_report_agent.config.persona import PersonaReviewRecoveryFailureMessages
from arrest_report_agent.config.review_failures import review_recovery_failure_messages
from arrest_report_agent.infrastructure.discord.config_admin import FEATURE_LABELS


@pytest.mark.parametrize(
    "payload",
    [
        {"role_ids": [0]},
        {"user_ids": [-1]},
        {"role_ids": ["*"]},
        {"user_ids": ["449044161954447360"]},
        {"role_ids": [True]},
        {"user_ids": [2**64]},
        {"role_ids": [123, 123]},
        {"user_ids": [123, 123]},
        {"role_ids": list(range(1, 27))},
        {"user_ids": list(range(1, 27))},
        {"unexpected": [123]},
    ],
)
def test_recipient_whitelists_reject_invalid_or_unbounded_options(payload):
    with pytest.raises(ValueError):
        ReviewFailureNotificationOptions.model_validate(payload)


def test_whitelist_feature_is_editable_and_runtime_respects_enabled_state(settings):
    values = {
        field: getattr(settings, field)
        for field in GuildAgentConfig.model_fields
        if field not in {"model", "reasoning_effort"}
    }
    agent = GuildAgentConfig(
        model=settings.openai_model, reasoning_effort=settings.openai_reasoning_effort, **values
    )
    config = {
        "guild_id": 10,
        "revision_id": uuid4(),
        "revision": 1,
        "agent": agent,
        "policies": {
            key: AccessPolicySnapshot(
                key=key, subject_type=POLICY_SUBJECT_TYPES[key], mode=AccessPolicyMode.DISABLED
            )
            for key in AccessPolicyKey
        },
        "features": {},
        "persona_revision_id": uuid4(),
        "persona_id": "roy_velazco",
        "persona_revision": 66,
    }
    assert (
        GuildRuntimeSettings(
            settings, GuildConfigSnapshot.model_validate(config)
        ).review_failure_notification_options
        is None
    )
    for enabled in (False, True):
        config["features"] = {
            "review_failure_notifications": GuildFeatureSnapshot(
                key="review_failure_notifications",
                enabled=enabled,
                schema_version=1,
                payload={"role_ids": [123, 456], "user_ids": [789]},
            )
        }
        runtime = GuildRuntimeSettings(settings, GuildConfigSnapshot.model_validate(config))
        assert runtime.review_failure_notification_options == (
            ReviewFailureNotificationOptions(role_ids=(123, 456), user_ids=(789,)) if enabled else None
        )
    config["features"]["review_failure_notifications"] = GuildFeatureSnapshot(
        key="review_failure_notifications",
        enabled=True,
        schema_version=2,
        payload={},
    )
    with pytest.raises(ValueError, match="schema version 1"):
        GuildConfigSnapshot.model_validate(config)
    assert "review_failure_notifications" in FEATURE_LABELS
    assert len(FEATURE_LABELS) <= 25


@pytest.mark.asyncio
@pytest.mark.parametrize("persona_id", ["roy_velazco", "walter_vigil"])
async def test_legacy_database_personas_inherit_only_the_new_packaged_failure_copy(persona_id):
    profile = (await PackagedPersonaProvider().load(persona_id)).profile
    payload = profile.model_dump()
    payload["messages"]["failures"].pop("review_recovery_exhausted")
    payload["messages"]["failures"]["review_failure_title"] = "Título personalizado"
    legacy = BotPersona.model_validate(payload)

    assert legacy.messages.failures.review_recovery_exhausted is None
    assert review_recovery_failure_messages(legacy) == profile.messages.failures.review_recovery_exhausted
    assert legacy.messages.failures.review_failure_title == "Título personalizado"
    payload["messages"]["failures"]["review_recovery_exhausted"] = {
        "description": "Copia personalizada de BD.",
        "next_step": "Paso configurado.",
    }
    configured = BotPersona.model_validate(payload)
    assert review_recovery_failure_messages(configured).description == "Copia personalizada de BD."


def test_custom_persona_without_packaged_copy_has_safe_neutral_defaults(persona):
    custom = persona.model_copy(
        update={
            "id": "custom_persona",
            "messages": persona.messages.model_copy(
                update={
                    "failures": persona.messages.failures.model_copy(
                        update={"review_recovery_exhausted": None}
                    ),
                }
            ),
        }
    )
    assert review_recovery_failure_messages(custom) == PersonaReviewRecoveryFailureMessages()

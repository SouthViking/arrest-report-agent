from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import discord
import pytest
from aiohttp.test_utils import TestClient, TestServer

from arrest_report_agent.bootstrap.health import create_http_application
from arrest_report_agent.config import AccessPolicyKey, AccessPolicyMode
from arrest_report_agent.deployments.discord import DeploymentNotificationWorker, build_deployment_embed
from arrest_report_agent.deployments.models import DeploymentEventType, RailwayDeploymentEvent
from arrest_report_agent.deployments.repository import DeploymentEventRecord
from arrest_report_agent.release import ReleaseManifest


def railway_payload(event_type: str = "Deployment.deployed") -> dict[str, object]:
    return {
        "type": event_type,
        "details": {
            "id": "detail-id",
            "source": "GitHub",
            "status": "SUCCESS",
            "branch": "main",
            "commitHash": "a" * 40,
            "commitAuthor": "SouthViking",
            "commitMessage": "Ship deployment notifications",
        },
        "resource": {
            "workspace": {"id": "workspace", "name": "Personal"},
            "project": {"id": "project", "name": "Agent"},
            "environment": {
                "id": "production",
                "name": "production",
                "isEphemeral": False,
            },
            "service": {"id": "bot", "name": "app"},
            "deployment": {"id": "deployment-1"},
        },
        "severity": "INFO",
        "timestamp": "2026-08-12T12:34:56.000Z",
    }


def test_railway_payload_normalizes_supported_events_and_identity() -> None:
    event = RailwayDeploymentEvent.model_validate(railway_payload("Deployment.OOM_KILLED"))

    assert event.event_type is DeploymentEventType.OOM_KILLED
    assert event.deployment_id == "deployment-1"
    assert event.matches_runtime(
        project_id="project",
        environment_id="production",
        service_id="bot",
    )
    assert event.matches_commit("a" * 8)
    assert len(event.provider_event_key) == 64


def test_release_manifest_is_packaged_and_matches_application_version() -> None:
    manifest = ReleaseManifest.load_packaged()

    assert manifest.version == "0.27.1"
    assert manifest.repository_url == "https://github.com/SouthViking/arrest-report-agent"
    assert manifest.changelog_targets == (1241206587079524442,)
    assert manifest.changes
    rendered_changes = " ".join(change.text for change in manifest.changes)
    assert "tool call redundante" in rendered_changes
    assert "fotografías y respaldos" in rendered_changes
    assert manifest.acknowledgements == ()


def test_release_manifest_rejects_non_github_repository() -> None:
    payload = ReleaseManifest.load_packaged().model_dump(mode="json")
    payload["repository_url"] = "https://example.com/SouthViking/arrest-report-agent"

    with pytest.raises(ValueError, match="GitHub repository"):
        ReleaseManifest.model_validate(payload)


@pytest.mark.parametrize(
    "acknowledgements",
    [
        [{"type": "suggestion", "user_id": 0, "text": "Idea"}],
        [{"type": "unknown", "user_id": 123, "text": "Idea"}],
        [{"type": "testing", "user_id": 123, "text": ""}],
    ],
)
def test_release_manifest_rejects_invalid_acknowledgements(
    acknowledgements: object,
) -> None:
    payload = ReleaseManifest.load_packaged().model_dump(mode="json")
    payload["acknowledgements"] = acknowledgements

    with pytest.raises(ValueError):
        ReleaseManifest.model_validate(payload)


@pytest.mark.parametrize("targets", [[123, 123], [0], [True], "123"])
def test_release_manifest_rejects_invalid_changelog_targets(targets: object) -> None:
    payload = ReleaseManifest.load_packaged().model_dump(mode="json")
    payload["changelog_targets"] = targets

    with pytest.raises(ValueError, match="changelog_targets"):
        ReleaseManifest.model_validate(payload)


def test_deployment_embeds_keep_operational_data_and_persona_framing(persona) -> None:
    manifest = ReleaseManifest.load_packaged()
    event = DeploymentEventRecord(
        id=UUID("00000000-0000-0000-0000-000000000001"),
        event_type=DeploymentEventType.DEPLOYED,
        deployment_id="deployment-1",
        project_id="project",
        environment_id="production",
        service_id="bot",
        source="GitHub",
        branch="main",
        commit_hash="abcdef1234567890",
        commit_message="message",
        severity="INFO",
        timestamp=datetime(2026, 8, 12, 12, 34, tzinfo=UTC),
    )

    embed = build_deployment_embed(event, manifest, persona)

    assert embed.title == "🚀 Revisión estructurada más precisa · v0.27.1"
    assert persona.messages.interface.deployments.release_intro in (embed.description or "")
    assert any(field.name == "Cambios" for field in embed.fields)
    assert all(field.name != "Agradecimientos" for field in embed.fields)
    deployment_field = next(field for field in embed.fields if field.name == "Despliegue")
    assert (
        "[abcdef12](https://github.com/SouthViking/arrest-report-agent/commit/"
        "abcdef1234567890)" in deployment_field.value
    )
    assert embed.colour == discord.Color.green()


def test_deployment_embed_does_not_link_untrusted_commit_metadata(persona) -> None:
    manifest = ReleaseManifest.load_packaged()
    event = DeploymentEventRecord(
        id=UUID("00000000-0000-0000-0000-000000000006"),
        event_type=DeploymentEventType.FAILED,
        deployment_id="deployment-untrusted",
        project_id="project",
        environment_id="production",
        service_id="bot",
        source="Manual",
        branch="main",
        commit_hash="abcdef1234567890",
        commit_message=None,
        severity="ERROR",
        timestamp=datetime(2026, 8, 12, 12, 34, tzinfo=UTC),
    )

    embed = build_deployment_embed(event, manifest, persona)

    deployment_field = next(field for field in embed.fields if field.name == "Despliegue")
    assert "**Commit:** `abcdef12`" in deployment_field.value
    assert "https://" not in deployment_field.value


class FakeReceiver:
    secret = "s" * 43

    def __init__(self) -> None:
        self.events: list[RailwayDeploymentEvent] = []

    async def receive(self, event: RailwayDeploymentEvent) -> SimpleNamespace:
        self.events.append(event)
        return SimpleNamespace(status="accepted")


class FakeDeploymentRepository:
    def __init__(self) -> None:
        self.ignored: list[tuple[UUID, str]] = []
        self.processed: list[UUID] = []
        self.targets: list[object] = []

    async def mark_event_ignored(self, event_id: UUID, reason: str) -> None:
        self.ignored.append((event_id, reason))

    async def enqueue_deliveries(self, _event_id: UUID, targets: tuple[object, ...]) -> int:
        self.targets.extend(targets)
        return len(targets)

    async def mark_event_processed(self, event_id: UUID) -> None:
        self.processed.append(event_id)


@pytest.mark.asyncio
async def test_success_event_waits_for_the_matching_runtime_and_then_targets_channels() -> None:
    event = DeploymentEventRecord(
        id=UUID("00000000-0000-0000-0000-000000000002"),
        event_type=DeploymentEventType.DEPLOYED,
        deployment_id="deployment-2",
        project_id="project",
        environment_id="production",
        service_id="bot",
        source="GitHub",
        branch="main",
        commit_hash="b" * 40,
        commit_message=None,
        severity="INFO",
        timestamp=datetime.now(UTC),
    )
    repository = FakeDeploymentRepository()
    policy = SimpleNamespace(mode=AccessPolicyMode.RESTRICTED, ids=(456, 789))
    bundle = SimpleNamespace(
        snapshot=SimpleNamespace(
            features={"deployment_notifications": SimpleNamespace(enabled=True)},
            policies={AccessPolicyKey.DEPLOYMENT_NOTIFICATION_CHANNELS: policy},
        )
    )
    provider = SimpleNamespace(
        guild_ids=lambda: _async_value((123,)),
        cached=lambda _guild_id: _async_value(bundle),
    )
    manifest_payload = ReleaseManifest.load_packaged().model_dump(mode="json")
    manifest_payload["changelog_targets"] = "all"
    manifest = ReleaseManifest.model_validate(manifest_payload)
    old_worker = DeploymentNotificationWorker(
        SimpleNamespace(),  # type: ignore[arg-type]
        provider,  # type: ignore[arg-type]
        repository,  # type: ignore[arg-type]
        manifest,
        current_commit_sha="a" * 40,
    )

    await old_worker._prepare_event(event)  # noqa: SLF001

    assert repository.processed == []
    assert repository.targets == []

    new_worker = DeploymentNotificationWorker(
        SimpleNamespace(),  # type: ignore[arg-type]
        provider,  # type: ignore[arg-type]
        repository,  # type: ignore[arg-type]
        manifest,
        current_commit_sha="b" * 40,
    )
    await new_worker._prepare_event(event)  # noqa: SLF001

    assert repository.processed == [event.id]
    assert [target.channel_id for target in repository.targets] == [456, 789]
    assert all(target.notification_key.endswith(":0.27.1") for target in repository.targets)


@pytest.mark.asyncio
async def test_success_changelog_targets_only_selected_configured_channels() -> None:
    event = DeploymentEventRecord(
        id=UUID("00000000-0000-0000-0000-000000000007"),
        event_type=DeploymentEventType.DEPLOYED,
        deployment_id="deployment-selected",
        project_id="project",
        environment_id="production",
        service_id="bot",
        source="GitHub",
        branch="main",
        commit_hash="b" * 40,
        commit_message=None,
        severity="INFO",
        timestamp=datetime.now(UTC),
    )
    repository = FakeDeploymentRepository()
    policy = SimpleNamespace(mode=AccessPolicyMode.RESTRICTED, ids=(456, 789))
    bundle = SimpleNamespace(
        snapshot=SimpleNamespace(
            features={"deployment_notifications": SimpleNamespace(enabled=True)},
            policies={AccessPolicyKey.DEPLOYMENT_NOTIFICATION_CHANNELS: policy},
        )
    )
    provider = SimpleNamespace(
        guild_ids=lambda: _async_value((123,)),
        cached=lambda _guild_id: _async_value(bundle),
    )
    payload = ReleaseManifest.load_packaged().model_dump(mode="json")
    payload["changelog_targets"] = [789, 999]
    manifest = ReleaseManifest.model_validate(payload)
    worker = DeploymentNotificationWorker(
        SimpleNamespace(),  # type: ignore[arg-type]
        provider,  # type: ignore[arg-type]
        repository,  # type: ignore[arg-type]
        manifest,
        current_commit_sha="b" * 40,
    )

    await worker._prepare_event(event)  # noqa: SLF001

    assert [target.channel_id for target in repository.targets] == [789]


@pytest.mark.asyncio
async def test_empty_changelog_targets_suppress_success_announcement() -> None:
    event = DeploymentEventRecord(
        id=UUID("00000000-0000-0000-0000-000000000009"),
        event_type=DeploymentEventType.DEPLOYED,
        deployment_id="deployment-silent",
        project_id="project",
        environment_id="production",
        service_id="bot",
        source="GitHub",
        branch="main",
        commit_hash="b" * 40,
        commit_message=None,
        severity="INFO",
        timestamp=datetime.now(UTC),
    )
    repository = FakeDeploymentRepository()
    policy = SimpleNamespace(mode=AccessPolicyMode.RESTRICTED, ids=(456, 789))
    bundle = SimpleNamespace(
        snapshot=SimpleNamespace(
            features={"deployment_notifications": SimpleNamespace(enabled=True)},
            policies={AccessPolicyKey.DEPLOYMENT_NOTIFICATION_CHANNELS: policy},
        )
    )
    provider = SimpleNamespace(
        guild_ids=lambda: _async_value((123,)),
        cached=lambda _guild_id: _async_value(bundle),
    )
    manifest_payload = ReleaseManifest.load_packaged().model_dump(mode="json")
    manifest_payload["changelog_targets"] = []
    worker = DeploymentNotificationWorker(
        SimpleNamespace(),  # type: ignore[arg-type]
        provider,  # type: ignore[arg-type]
        repository,  # type: ignore[arg-type]
        ReleaseManifest.model_validate(manifest_payload),
        current_commit_sha="b" * 40,
    )

    await worker._prepare_event(event)  # noqa: SLF001

    assert repository.targets == []
    assert repository.processed == []
    assert repository.ignored == [(event.id, "no_configured_targets")]


@pytest.mark.asyncio
async def test_failure_alerts_use_only_dedicated_incident_channels() -> None:
    event = DeploymentEventRecord(
        id=UUID("00000000-0000-0000-0000-000000000008"),
        event_type=DeploymentEventType.FAILED,
        deployment_id="deployment-failed",
        project_id="project",
        environment_id="production",
        service_id="bot",
        source="GitHub",
        branch="main",
        commit_hash="b" * 40,
        commit_message=None,
        severity="ERROR",
        timestamp=datetime.now(UTC),
    )
    repository = FakeDeploymentRepository()
    release_policy = SimpleNamespace(mode=AccessPolicyMode.RESTRICTED, ids=(456, 789))
    incident_policy = SimpleNamespace(mode=AccessPolicyMode.RESTRICTED, ids=(789,))
    bundle = SimpleNamespace(
        snapshot=SimpleNamespace(
            features={"deployment_notifications": SimpleNamespace(enabled=True)},
            policies={
                AccessPolicyKey.DEPLOYMENT_NOTIFICATION_CHANNELS: release_policy,
                AccessPolicyKey.DEPLOYMENT_INCIDENT_NOTIFICATION_CHANNELS: incident_policy,
            },
        )
    )
    provider = SimpleNamespace(
        guild_ids=lambda: _async_value((123,)),
        cached=lambda _guild_id: _async_value(bundle),
    )
    payload = ReleaseManifest.load_packaged().model_dump(mode="json")
    payload["changelog_targets"] = [789]
    worker = DeploymentNotificationWorker(
        SimpleNamespace(),  # type: ignore[arg-type]
        provider,  # type: ignore[arg-type]
        repository,  # type: ignore[arg-type]
        ReleaseManifest.model_validate(payload),
        current_commit_sha="b" * 40,
    )

    await worker._prepare_event(event)  # noqa: SLF001

    assert [target.channel_id for target in repository.targets] == [789]


async def _async_value(value: object) -> object:
    return value


@pytest.mark.asyncio
async def test_webhook_endpoint_authenticates_validates_and_limits_cors() -> None:
    async def healthy() -> dict[str, object]:
        return {"ok": True}

    receiver = FakeReceiver()
    app = create_http_application(healthy, receiver)  # type: ignore[arg-type]
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        wrong = await client.post(
            "/webhooks/railway/wrong",
            json=railway_payload(),
        )
        assert wrong.status == 404

        invalid = await client.post(
            f"/webhooks/railway/{receiver.secret}",
            data="not-json",
            headers={"Content-Type": "application/json"},
        )
        assert invalid.status == 400

        accepted = await client.post(
            f"/webhooks/railway/{receiver.secret}",
            json=railway_payload(),
            headers={"Origin": "https://railway.com"},
        )
        assert accepted.status == 202
        assert await accepted.json() == {"status": "accepted"}
        assert accepted.headers["Access-Control-Allow-Origin"] == "https://railway.com"
        assert len(receiver.events) == 1

        untrusted = await client.post(
            f"/webhooks/railway/{receiver.secret}",
            json=railway_payload(),
            headers={"Origin": "https://example.com"},
        )
        assert untrusted.status == 202
        assert "Access-Control-Allow-Origin" not in untrusted.headers
    finally:
        await client.close()

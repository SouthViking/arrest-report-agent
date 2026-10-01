from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import discord
import pytest
from aiohttp.test_utils import TestClient, TestServer

from arrest_report_agent import __version__
from arrest_report_agent.bootstrap.health import create_http_application
from arrest_report_agent.config import AccessPolicyKey, AccessPolicyMode
from arrest_report_agent.deployments.discord import DeploymentNotificationWorker, build_deployment_embed
from arrest_report_agent.deployments.media import (
    CHANGELOG_THUMBNAIL_FILENAME,
    CHANGELOG_THUMBNAIL_URL,
    changelog_thumbnail_file,
)
from arrest_report_agent.deployments.models import DeploymentEventType, RailwayDeploymentEvent
from arrest_report_agent.deployments.repository import DeploymentDeliveryRecord, DeploymentEventRecord
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

    assert manifest.version == __version__
    assert manifest.repository_url == "https://github.com/SouthViking/arrest-report-agent"
    assert manifest.changelog_targets == (1241206587079524442,)
    assert manifest.targets_changelog_channel(1241206587079524442)
    assert not manifest.targets_changelog_channel(1234675609536430152)
    assert manifest.changes
    rendered_changes = " ".join(change.text for change in manifest.changes)
    assert "/solicitar-roles" in rendered_changes
    assert "cancelan las pendientes anteriores" in rendered_changes
    assert "Command/Executive" in rendered_changes
    assert "/info-roles" in rendered_changes
    assert all(word not in rendered_changes.lower() for word in ("thumbnail", "imagen", "migración"))
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

    assert embed.title == f"🚀 {manifest.title} · v{manifest.version}"
    assert persona.messages.interface.deployments.release_intro in (embed.description or "")
    assert any(field.name == "Cambios" for field in embed.fields)
    assert all(field.name != "Agradecimientos" for field in embed.fields)
    deployment_field = next(field for field in embed.fields if field.name == "Despliegue")
    assert (
        "[abcdef12](https://github.com/SouthViking/arrest-report-agent/commit/"
        "abcdef1234567890)" in deployment_field.value
    )
    assert embed.colour == discord.Color.green()
    assert embed.thumbnail.url == CHANGELOG_THUMBNAIL_URL


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
    assert not embed.thumbnail


def test_changelog_thumbnail_keeps_original_bytes_and_uses_independent_streams():
    first = changelog_thumbnail_file()
    second = changelog_thumbnail_file()
    try:
        assert first.filename == second.filename == CHANGELOG_THUMBNAIL_FILENAME
        assert first.fp is not second.fp
        original = first.fp.read()
        assert len(original) == 24150
        assert hashlib.sha256(original).hexdigest() == (
            "e5d0f19f95b886225b3911169d7ad558675dc43e107897452b2f294835a1d63b"
        )
        assert second.fp.read() == original
    finally:
        first.close()
        second.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("event_type", tuple(DeploymentEventType))
@pytest.mark.parametrize("can_attach", (False, True))
async def test_deployment_delivery_thumbnail_is_only_for_changelog_and_optional(
    persona, monkeypatch, caplog, event_type, can_attach,
):
    event = DeploymentEventRecord(
        id=UUID(int=1), event_type=event_type, deployment_id="deploy",
        project_id="project", environment_id="production", service_id="bot",
        source="GitHub", branch="main", commit_hash="a" * 40, commit_message=None,
        severity="INFO", timestamp=datetime.now(UTC),
    )
    delivery = DeploymentDeliveryRecord(
        id=UUID(int=2), guild_id=123, channel_id=456,
        kind="release" if event_type is DeploymentEventType.DEPLOYED else "incident",
        attempts=1, event=event,
    )
    channel = SimpleNamespace(
        guild=SimpleNamespace(me=object()),
        permissions_for=lambda _: SimpleNamespace(attach_files=can_attach),
        send=AsyncMock(return_value=SimpleNamespace(id=789)),
    )
    repository = SimpleNamespace(mark_delivery_sent=AsyncMock(), mark_delivery_failed=AsyncMock())
    provider = SimpleNamespace(cached=AsyncMock(
        return_value=SimpleNamespace(persona=SimpleNamespace(profile=persona)),
    ))
    worker = DeploymentNotificationWorker(
        SimpleNamespace(), provider, repository, ReleaseManifest.load_packaged(), current_commit_sha="a" * 40,
    )
    monkeypatch.setattr(worker, "_resolve_channel", AsyncMock(return_value=channel))
    await worker._deliver(delivery)
    repository.mark_delivery_sent.assert_awaited_once_with(delivery.id, 789)
    repository.mark_delivery_failed.assert_not_awaited()
    message = channel.send.call_args.kwargs
    if event_type is DeploymentEventType.DEPLOYED and can_attach:
        assert message["embed"].thumbnail.url == CHANGELOG_THUMBNAIL_URL
        assert len(message["files"]) == 1
        assert message["files"][0].filename == CHANGELOG_THUMBNAIL_FILENAME
        message["files"][0].close()
    else:
        assert not message["embed"].thumbnail
        assert message["files"] == []
    if event_type is DeploymentEventType.DEPLOYED and not can_attach:
        assert "deployment_changelog_thumbnail_skipped" in caplog.text


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
    assert all(
        target.notification_key.endswith(f":{manifest.version}") for target in repository.targets
    )


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

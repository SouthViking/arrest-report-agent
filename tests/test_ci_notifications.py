from __future__ import annotations

import hashlib
import hmac
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import discord
import pytest
from aiohttp.test_utils import TestClient, TestServer

from arrest_report_agent.bootstrap.health import create_http_application
from arrest_report_agent.ci_notifications.discord import (
    CiNotificationWorker,
    build_ci_failure_embed,
)
from arrest_report_agent.ci_notifications.models import GitHubWorkflowRunEvent
from arrest_report_agent.ci_notifications.repository import CiWorkflowEventRecord
from arrest_report_agent.ci_notifications.webhook import GitHubWebhookReceiver
from arrest_report_agent.config import AccessPolicyKey, AccessPolicyMode, AppSettings
from arrest_report_agent.release import ReleaseManifest

SECRET = "github-webhook-secret-value-1234567890"


def github_payload(
    *,
    conclusion: str = "failure",
    workflow: str = "CI",
    branch: str = "feature/anything",
) -> dict[str, object]:
    return {
        "action": "completed",
        "repository": {
            "id": 123456,
            "full_name": "SouthViking/arrest-report-agent",
        },
        "sender": {"login": "SouthViking"},
        "workflow_run": {
            "id": 31741783031,
            "workflow_id": 9001,
            "name": workflow,
            "event": "push",
            "status": "completed",
            "conclusion": conclusion,
            "run_attempt": 1,
            "head_branch": branch,
            "head_sha": "a" * 40,
            "actor": {"login": "SouthViking"},
            "head_commit": {"message": "Break the migration\n\nDetails"},
            "run_started_at": "2026-08-13T20:35:00Z",
            "updated_at": "2026-08-13T20:37:30Z",
        },
    }


class FakeCiRepository:
    def __init__(self) -> None:
        self.events: list[tuple[str, GitHubWorkflowRunEvent]] = []
        self.targets: list[object] = []
        self.processed: list[UUID] = []
        self.ignored: list[tuple[UUID, str]] = []

    async def record_event(
        self,
        delivery_id: str,
        event: GitHubWorkflowRunEvent,
    ) -> tuple[object, bool]:
        self.events.append((delivery_id, event))
        return object(), len(self.events) == 1

    async def enqueue_deliveries(self, _event_id: UUID, targets: tuple[object, ...]) -> int:
        self.targets.extend(targets)
        return len(targets)

    async def mark_event_processed(self, event_id: UUID) -> None:
        self.processed.append(event_id)

    async def mark_event_ignored(self, event_id: UUID, reason: str) -> None:
        self.ignored.append((event_id, reason))


def webhook_settings() -> AppSettings:
    return AppSettings(
        _env_file=None,
        DISCORD_TOKEN="discord",
        OPENAI_API_KEY="openai",
        GITHUB_WEBHOOK_SECRET=SECRET,
    )


def signature(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


@pytest.mark.asyncio
async def test_native_github_webhook_requires_signature_and_accepts_any_branch() -> None:
    async def healthy() -> dict[str, object]:
        return {"ok": True}

    repository = FakeCiRepository()
    wakeups: list[bool] = []
    receiver = GitHubWebhookReceiver(
        webhook_settings(),
        repository,  # type: ignore[arg-type]
        lambda: wakeups.append(True),
        expected_repository="SouthViking/arrest-report-agent",
    )
    client = TestClient(TestServer(create_http_application(healthy, None, receiver)))
    await client.start_server()
    try:
        body = json.dumps(github_payload(branch="fix/from-any-branch")).encode()
        unsigned = await client.post(
            "/webhooks/github/actions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-GitHub-Event": "workflow_run",
                "X-GitHub-Delivery": "11111111-1111-1111-1111-111111111111",
            },
        )
        assert unsigned.status == 401

        accepted = await client.post(
            "/webhooks/github/actions",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-GitHub-Event": "workflow_run",
                "X-GitHub-Delivery": "11111111-1111-1111-1111-111111111111",
                "X-Hub-Signature-256": signature(body),
            },
        )
        assert accepted.status == 202
        assert await accepted.json() == {"status": "accepted"}
        assert repository.events[0][1].workflow_run.head_branch == "fix/from-any-branch"
        assert wakeups == [True]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_webhook_ignores_successes_other_workflows_and_ping() -> None:
    repository = FakeCiRepository()
    receiver = GitHubWebhookReceiver(
        webhook_settings(),
        repository,  # type: ignore[arg-type]
        lambda: None,
        expected_repository="SouthViking/arrest-report-agent",
    )
    for delivery_id, event_name, payload in (
        ("success", "workflow_run", github_payload(conclusion="success")),
        ("other", "workflow_run", github_payload(workflow="Security")),
        ("ping", "ping", {"zen": "Keep it logically awesome."}),
    ):
        body = json.dumps(payload).encode()
        result = await receiver.receive(
            body,
            event_name=event_name,
            delivery_id=delivery_id,
            signature=signature(body),
        )
        assert result.status == "ignored"
    assert repository.events == []


@pytest.mark.asyncio
async def test_ci_worker_uses_only_the_ci_failure_channel_policy() -> None:
    event = CiWorkflowEventRecord(
        id=UUID("00000000-0000-0000-0000-000000000001"),
        repository_id=123456,
        repository_full_name="SouthViking/arrest-report-agent",
        workflow_id=9001,
        workflow_name="CI",
        run_id=31741783031,
        run_attempt=1,
        triggering_event="push",
        branch="anything",
        commit_hash="a" * 40,
        commit_message="Break the migration",
        actor_login="SouthViking",
        timestamp=datetime.now(UTC),
    )
    repository = FakeCiRepository()
    ci_policy = SimpleNamespace(mode=AccessPolicyMode.RESTRICTED, ids=(111,))
    deployment_policy = SimpleNamespace(mode=AccessPolicyMode.RESTRICTED, ids=(222, 333))
    bundle = SimpleNamespace(
        snapshot=SimpleNamespace(
            policies={
                AccessPolicyKey.CI_FAILURE_NOTIFICATION_CHANNELS: ci_policy,
                AccessPolicyKey.DEPLOYMENT_NOTIFICATION_CHANNELS: deployment_policy,
            }
        )
    )
    provider = SimpleNamespace(
        guild_ids=lambda: _async_value((123,)),
        cached=lambda _guild_id: _async_value(bundle),
    )
    worker = CiNotificationWorker(
        SimpleNamespace(),  # type: ignore[arg-type]
        provider,  # type: ignore[arg-type]
        repository,  # type: ignore[arg-type]
        ReleaseManifest.load_packaged(),
    )

    await worker._prepare_event(event)  # noqa: SLF001

    assert [target.channel_id for target in repository.targets] == [111]
    assert repository.processed == [event.id]


def test_ci_failure_embed_contains_safe_links_without_logs(persona) -> None:
    event = CiWorkflowEventRecord(
        id=UUID("00000000-0000-0000-0000-000000000002"),
        repository_id=123456,
        repository_full_name="SouthViking/arrest-report-agent",
        workflow_id=9001,
        workflow_name="CI",
        run_id=31741783031,
        run_attempt=2,
        triggering_event="pull_request",
        branch="feature/example",
        commit_hash="a" * 40,
        commit_message="Break the migration\nsecret-looking details",
        actor_login="SouthViking",
        timestamp=datetime(2026, 8, 13, 20, 37, 30, tzinfo=UTC),
    )

    embed = build_ci_failure_embed(event, ReleaseManifest.load_packaged(), persona)

    assert embed.title == "🚨 Falló la integración continua"
    assert persona.messages.interface.deployments.ci_failed_description == embed.description
    rendered = "\n".join(str(field.value) for field in embed.fields)
    assert "/actions/runs/31741783031" in rendered
    assert "/commit/" + "a" * 40 in rendered
    assert "secret-looking details" not in rendered
    assert embed.colour == discord.Color.red()


async def _async_value(value: object) -> object:
    return value

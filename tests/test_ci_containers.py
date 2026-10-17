from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from unittest.mock import Mock, call

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "ci_containers.py"
SPEC = importlib.util.spec_from_file_location("ci_containers", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
ci = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ci)


def completed(code: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], code)


def test_image_pull_stops_at_the_first_available_registry(monkeypatch) -> None:
    docker = Mock(return_value=completed())
    monkeypatch.setattr(ci, "docker", docker)

    image = ci.pull_official_image("postgres", "16-alpine")

    assert image == "public.ecr.aws/docker/library/postgres:16-alpine"
    docker.assert_called_once_with("pull", image)


@pytest.mark.parametrize(
    "failure",
    [completed(1), subprocess.TimeoutExpired(["docker", "pull"], timeout=120)],
    ids=["registry-throttled", "registry-timeout"],
)
@pytest.mark.parametrize("repository,tag", [("postgres", "16-alpine"), ("python", "3.12-slim")])
def test_image_pull_uses_docker_hub_when_ecr_fails(monkeypatch, failure, repository, tag) -> None:
    docker = Mock(side_effect=[failure, completed()])
    monkeypatch.setattr(ci, "docker", docker)

    image = ci.pull_official_image(repository, tag)

    assert image == f"docker.io/library/{repository}:{tag}"
    assert docker.call_args_list == [
        call("pull", f"public.ecr.aws/docker/library/{repository}:{tag}"),
        call("pull", image),
    ]


def test_image_pull_fails_if_no_registry_is_available(monkeypatch) -> None:
    docker = Mock(return_value=completed(1))
    monkeypatch.setattr(ci, "docker", docker)

    with pytest.raises(RuntimeError, match="from any registry"):
        ci.pull_official_image("postgres", "16-alpine")

    assert docker.call_count == len(ci.REGISTRIES)


def test_postgres_waits_for_readiness_with_the_existing_test_configuration(monkeypatch) -> None:
    monkeypatch.setattr(ci, "pull_official_image", Mock(return_value="test-postgres"))
    docker = Mock(side_effect=[completed(), completed(1), completed()])
    sleep = Mock()
    monkeypatch.setattr(ci, "docker", docker)
    monkeypatch.setattr(ci.time, "sleep", sleep)

    ci.start_postgres()

    command = docker.call_args_list[0].args
    assert command[-1] == "test-postgres"
    assert "127.0.0.1:5432:5432" in command
    assert "POSTGRES_DB=arrest_report_agent_test" in command
    assert "POSTGRES_HOST_AUTH_METHOD=trust" in command
    assert docker.call_args_list[-1] == call(
        "exec", ci.POSTGRES_CONTAINER, "pg_isready", "-U", "postgres", timeout=5
    )
    sleep.assert_called_once_with(2)


def test_postgres_start_failure_does_not_proceed_to_the_tests(monkeypatch) -> None:
    monkeypatch.setattr(ci, "pull_official_image", Mock(return_value="test-postgres"))
    docker = Mock(return_value=completed(1))
    monkeypatch.setattr(ci, "docker", docker)

    with pytest.raises(subprocess.CalledProcessError):
        ci.start_postgres()

    docker.assert_called_once()


def test_postgres_fails_if_readiness_is_never_reached(monkeypatch) -> None:
    monkeypatch.setattr(ci, "pull_official_image", Mock(return_value="test-postgres"))
    docker = Mock(side_effect=[completed(), *[completed(1) for _ in range(15)], completed()])
    monkeypatch.setattr(ci, "docker", docker)
    monkeypatch.setattr(ci.time, "sleep", Mock())

    with pytest.raises(RuntimeError, match="did not become ready"):
        ci.start_postgres()

    assert docker.call_count == 17
    docker.assert_called_with("logs", ci.POSTGRES_CONTAINER)


def test_build_uses_the_selected_official_base_and_propagates_a_real_failure(monkeypatch) -> None:
    select = Mock(return_value="docker.io/library/python:3.12-slim")
    docker = Mock(return_value=completed(1))
    monkeypatch.setattr(ci, "pull_official_image", select)
    monkeypatch.setattr(ci, "docker", docker)

    with pytest.raises(subprocess.CalledProcessError):
        ci.build_runtime("arrest-report-agent:test")

    select.assert_called_once_with("python", "3.12-slim")
    docker.assert_called_once_with(
        "build",
        "--build-arg",
        "PYTHON_IMAGE=docker.io/library/python:3.12-slim",
        "--tag",
        "arrest-report-agent:test",
        ".",
        timeout=600,
    )

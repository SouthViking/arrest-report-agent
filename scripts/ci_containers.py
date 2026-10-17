"""Start isolated CI containers using alternative registries for official images."""

from __future__ import annotations

import argparse
import subprocess
import time

REGISTRIES = ("public.ecr.aws/docker/library", "docker.io/library")
POSTGRES_CONTAINER = "arrest-report-agent-ci-postgres"


def docker(*arguments: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *arguments], check=False, text=True, timeout=timeout)


def pull_official_image(repository: str, tag: str) -> str:
    for registry in REGISTRIES:
        image = f"{registry}/{repository}:{tag}"
        print(f"Pulling official image {image}", flush=True)
        try:
            result = docker("pull", image)
        except subprocess.TimeoutExpired:
            print(f"::warning::Pull timed out for {image}; trying the next registry", flush=True)
            continue
        if result.returncode == 0:
            return image
        print(f"::warning::Pull failed for {image}; trying the next registry", flush=True)
    raise RuntimeError(f"Unable to pull the official {repository}:{tag} image from any registry")


def start_postgres() -> None:
    image = pull_official_image("postgres", "16-alpine")
    docker(
        "run",
        "--detach",
        "--name",
        POSTGRES_CONTAINER,
        "--publish",
        "127.0.0.1:5432:5432",
        "--env",
        "POSTGRES_USER=postgres",
        "--env",
        "POSTGRES_DB=arrest_report_agent_test",
        "--env",
        "POSTGRES_HOST_AUTH_METHOD=trust",
        image,
    ).check_returncode()
    for attempt in range(15):
        try:
            ready = docker("exec", POSTGRES_CONTAINER, "pg_isready", "-U", "postgres", timeout=5)
        except subprocess.TimeoutExpired:
            ready = None
        if ready is not None and ready.returncode == 0:
            return
        if attempt < 14:
            time.sleep(2)
    docker("logs", POSTGRES_CONTAINER)
    raise RuntimeError("The isolated PostgreSQL container did not become ready")


def build_runtime(tag: str) -> None:
    image = pull_official_image("python", "3.12-slim")
    # Only pull failures select another registry. A real build failure must fail CI.
    docker("build", "--build-arg", f"PYTHON_IMAGE={image}", "--tag", tag, ".", timeout=600).check_returncode()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("postgres", help="start the isolated PostgreSQL test container")
    build = commands.add_parser("build", help="build the runtime with an available official Python image")
    build.add_argument("--tag", required=True)
    args = parser.parse_args()
    if args.command == "postgres":
        start_postgres()
    else:
        build_runtime(args.tag)


if __name__ == "__main__":
    main()

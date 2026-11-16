"""Shared Railway/S3 utilities for operator scripts, independent of the bot runtime."""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from contextlib import closing
from dataclasses import dataclass, field
from typing import Any, BinaryIO

import boto3
from botocore.config import Config

CHUNK_SIZE = 1024 * 1024
PROPERTY_NAMES = (
    "ContentType", "CacheControl", "ContentDisposition", "ContentEncoding",
    "ContentLanguage", "Expires", "WebsiteRedirectLocation",
)


class BucketError(Exception):
    """An actionable error whose message contains no credentials."""


@dataclass(frozen=True)
class BucketReference:
    project_id: str
    environment_id: str
    bucket_id: str


@dataclass(frozen=True)
class ObjectVersion:
    key: str
    size: int
    etag: str
    modified: Any


@dataclass
class Bucket:
    name: str
    client: Any = field(repr=False)

    def inventory(self, prefix: str = "") -> dict[str, ObjectVersion]:
        result = {}
        pages = self.client.get_paginator("list_objects_v2").paginate(Bucket=self.name, Prefix=prefix)
        for page in pages:
            for item in page.get("Contents", []):
                key = item["Key"]
                result[key] = ObjectVersion(key, item["Size"], item["ETag"], item["LastModified"])
        return result

    def tags(self, key: str) -> dict[str, str]:
        response = self.client.get_object_tagging(Bucket=self.name, Key=key)
        return {tag["Key"]: tag["Value"] for tag in response.get("TagSet", [])}


def load_buckets(source: BucketReference, destination: BucketReference | None = None) -> tuple[Bucket, ...]:
    executable = shutil.which("railway")
    if executable is None:
        raise BucketError("Instala Railway CLI y ejecuta railway login antes de continuar.")
    selections = []
    references = [("source", source)]
    if destination is not None:
        references.append(("destination", destination))
    for label, ref in references:
        arguments = ", ".join(
            f"{name}: {json.dumps(value)}" for name, value in (
                ("projectId", ref.project_id), ("environmentId", ref.environment_id),
                ("bucketId", ref.bucket_id),
            )
        )
        selections.append(
            f"{label}: bucketS3Credentials({arguments}) "
            "{ bucketName endpoint region urlStyle accessKeyId secretAccessKey }"
        )
    # A read-only query on explicit resources; no project linking or deployments.
    response = subprocess.run(
        [executable, "api", "--compact"], input="query { " + " ".join(selections) + " }",
        capture_output=True, text=True, encoding="utf-8", check=False, timeout=60,
    )
    if response.returncode:
        raise BucketError("Railway CLI no pudo consultar las credenciales; revisa sesión y permisos.")
    payload = json.loads(response.stdout)
    if payload.get("errors"):
        raise BucketError("Railway rechazó la consulta de credenciales; revisa los IDs y permisos.")
    buckets = []
    for label, _ in references:
        entries = payload["data"][label]
        if len(entries) != 1:
            raise BucketError("Se esperaba una sola instancia de bucket por entorno.")
        credentials = entries[0]
        style = {"path": "path", "virtual-host": "virtual", "virtual-hosted": "virtual", "virtual": "virtual"}
        if credentials["urlStyle"] not in style:
            raise BucketError("Railway devolvió un estilo de URL no reconocido.")
        client = boto3.client(
            "s3", endpoint_url=credentials["endpoint"], region_name=credentials["region"],
            aws_access_key_id=credentials["accessKeyId"],
            aws_secret_access_key=credentials["secretAccessKey"],
            config=Config(
                max_pool_connections=16, signature_version="s3v4",
                s3={"addressing_style": style[credentials["urlStyle"]]},
                retries={"mode": "standard", "max_attempts": 4}, connect_timeout=15, read_timeout=60,
                request_checksum_calculation="when_required", response_checksum_validation="when_required",
            ),
        )
        buckets.append(Bucket(credentials["bucketName"], client))
    if len(buckets) == 2 and buckets[0].name == buckets[1].name:
        raise BucketError("El origen y el destino deben ser buckets distintos.")
    return tuple(buckets)


def properties(response: dict[str, Any]) -> dict[str, Any]:
    result = {name: response[name] for name in PROPERTY_NAMES if name in response}
    result["Metadata"] = response.get("Metadata", {})
    return result


def read_object(
    bucket: Bucket, version: ObjectVersion, output: BinaryIO | None = None,
) -> tuple[str, dict[str, Any]]:
    response = bucket.client.get_object(Bucket=bucket.name, Key=version.key, IfMatch=version.etag)
    digest = hashlib.sha256()
    size = 0
    with closing(response["Body"]) as body:
        while chunk := body.read(CHUNK_SIZE):
            digest.update(chunk)
            size += len(chunk)
            if output is not None:
                output.write(chunk)
    fingerprint = digest.hexdigest()
    expected_hash = response.get("Metadata", {}).get("sha256")
    if size != version.size or (expected_hash and expected_hash.lower() != fingerprint):
        raise BucketError("Un objeto no coincide con su tamaño o SHA-256 declarado.")
    return fingerprint, properties(response)

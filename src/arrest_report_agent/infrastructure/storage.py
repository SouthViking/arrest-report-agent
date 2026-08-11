"""S3-compatible object storage used for immutable persona assets."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from typing import Protocol

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError


class ObjectStorageError(RuntimeError):
    """Raised when the configured object store cannot complete an operation."""


class ObjectStorage(Protocol):
    async def put(self, key: str, payload: bytes, content_type: str) -> None: ...

    async def get(self, key: str) -> bytes: ...

    async def exists(self, key: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class BucketConfig:
    bucket: str
    endpoint_url: str
    region: str
    access_key_id: str
    secret_access_key: str


class RailwayBucketStorage:
    """Small async facade over Railway's S3-compatible bucket API."""

    def __init__(self, config: BucketConfig) -> None:
        self.config = config
        self._client = boto3.client(
            "s3",
            endpoint_url=config.endpoint_url,
            region_name=config.region,
            aws_access_key_id=config.access_key_id,
            aws_secret_access_key=config.secret_access_key,
            config=Config(signature_version="s3v4", retries={"max_attempts": 3}),
        )

    async def put(self, key: str, payload: bytes, content_type: str) -> None:
        if not payload:
            raise ValueError("Object payload cannot be empty")
        try:
            await asyncio.to_thread(
                self._client.put_object,
                Bucket=self.config.bucket,
                Key=key,
                Body=payload,
                ContentType=content_type,
                Metadata={"sha256": hashlib.sha256(payload).hexdigest()},
            )
        except (BotoCoreError, ClientError, OSError) as error:
            raise ObjectStorageError(f"Unable to upload object '{key}'") from error

    async def get(self, key: str) -> bytes:
        try:
            response = await asyncio.to_thread(
                self._client.get_object,
                Bucket=self.config.bucket,
                Key=key,
            )
            return await asyncio.to_thread(response["Body"].read)
        except (BotoCoreError, ClientError, OSError, KeyError) as error:
            raise ObjectStorageError(f"Unable to read object '{key}'") from error

    async def exists(self, key: str) -> bool:
        try:
            await asyncio.to_thread(
                self._client.head_object,
                Bucket=self.config.bucket,
                Key=key,
            )
            return True
        except ClientError as error:
            status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status == 404:
                return False
            raise ObjectStorageError(f"Unable to inspect object '{key}'") from error
        except (BotoCoreError, OSError) as error:
            raise ObjectStorageError(f"Unable to inspect object '{key}'") from error


class MemoryObjectStorage:
    """Deterministic storage adapter for unit tests and local dry-runs."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}

    async def put(self, key: str, payload: bytes, content_type: str) -> None:
        self.objects[key] = (bytes(payload), content_type)

    async def get(self, key: str) -> bytes:
        try:
            return self.objects[key][0]
        except KeyError as error:
            raise ObjectStorageError(f"Unable to read object '{key}'") from error

    async def exists(self, key: str) -> bool:
        return key in self.objects

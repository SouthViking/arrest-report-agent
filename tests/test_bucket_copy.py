"""Exercise bucket migration safeguards without connecting to production."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import io
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import parse_qsl

import pytest
from botocore.exceptions import ClientError

SCRIPT_DIRECTORY = Path(__file__).resolve().parents[1] / "scripts" / "railway"
sys.path.insert(0, str(SCRIPT_DIRECTORY))
storage = importlib.import_module("bucket_storage")
SCRIPT = SCRIPT_DIRECTORY / "copy_railway_bucket.py"
SPEC = importlib.util.spec_from_file_location("copy_railway_bucket", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
migration = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = migration
SPEC.loader.exec_module(migration)


def object_data(body=b"photo", **properties):
    return {"body": body, "Metadata": {"sha256": hashlib.sha256(body).hexdigest()},
            "ContentType": "image/png", "tags": {}, **properties}


class MemoryS3:
    def __init__(self, objects=None):
        self.objects = copy.deepcopy(objects or {})
        self.reads = 0
        self.writes = 0
        self.corrupt_upload = False
        self.pages = 0

    def get_paginator(self, operation):
        assert operation == "list_objects_v2"
        return self

    def paginate(self, **kwargs):
        # Each object gets its own page to exercise full pagination.
        for key, item in sorted(self.objects.items()):
            self.pages += 1
            yield {"Contents": [{"Key": key, "Size": len(item["body"]),
                                 "ETag": self.etag(item), "LastModified": "fixed"}]}

    @staticmethod
    def etag(item):
        return '"' + hashlib.sha256(item["body"]).hexdigest() + '"'

    def get_object(self, *, Key, IfMatch, **kwargs):
        self.reads += 1
        item = self.objects[Key]
        assert IfMatch == self.etag(item)
        return {**copy.deepcopy(item), "Body": io.BytesIO(item["body"])}

    def get_object_tagging(self, *, Key, **kwargs):
        return {"TagSet": [{"Key": key, "Value": value}
                           for key, value in self.objects[Key]["tags"].items()]}

    def put_object(self, *, Key, Body, IfNoneMatch, ContentLength, Tagging="", **kwargs):
        assert IfNoneMatch == "*"
        assert Key not in self.objects
        self.writes += 1
        body = Body.read()
        assert len(body) == ContentLength
        body = b"corrupt" if self.corrupt_upload else body
        self.objects[Key] = {**kwargs, "body": body, "tags": dict(parse_qsl(Tagging))}
        self.objects[Key].pop("Bucket")

    def head_object(self, *, Key, **kwargs):
        item = self.objects[Key]
        return {"ContentLength": len(item["body"]), "ETag": self.etag(item), "LastModified": "fixed"}


def buckets(objects=None, target=None):
    return (migration.Bucket("source", MemoryS3(objects)),
            migration.Bucket("destination", MemoryS3(target)))


def test_default_plan_never_downloads_or_writes(capsys):
    source, destination = buckets({"a": object_data(), "b": object_data(b"other")})
    migration.migrate(source, destination)
    assert source.client.pages == 2
    assert source.client.reads == destination.client.reads == 0
    assert source.client.writes == destination.client.writes == 0
    assert "faltan 2" in capsys.readouterr().out


def test_copy_preserves_keys_content_metadata_tags_and_resumes_without_writes():
    objects = {
        "suspects/12/hash.png": object_data(
            tags={"case": "robo & recuperación"}, CacheControl="immutable",
            ContentDisposition='inline; filename="foto.png"', ContentLanguage="es",
        ),
        "support/34/hash.jpg": object_data(b"other"),
        "zero": object_data(b""),
    }
    source, destination = buckets(objects)
    migration.migrate(source, destination, "copy")
    assert destination.client.objects == objects
    assert destination.client.writes == 3
    migration.migrate(source, destination, "copy")
    migration.migrate(source, destination, "verify")
    assert destination.client.writes == 3
    assert source.client.writes == 0


@pytest.mark.parametrize("changed", [
    object_data(b"other"),
    object_data(ContentType="image/jpeg"),
    object_data(tags={"case": "different"}),
])
def test_existing_conflict_is_never_overwritten(changed):
    source, destination = buckets({"a": object_data()}, {"a": changed})
    before = copy.deepcopy(destination.client.objects)
    with pytest.raises(migration.MigrationError, match="difieren"):
        migration.migrate(source, destination, "copy")
    assert destination.client.objects == before
    assert destination.client.writes == 0


def test_source_sha256_mismatch_stops_before_upload():
    source, destination = buckets({"a": object_data(Metadata={"sha256": "0" * 64})})
    with pytest.raises(migration.MigrationError, match="SHA-256"):
        migration.migrate(source, destination, "copy")
    assert destination.client.writes == 0


def test_upload_is_read_back_and_checked():
    source, destination = buckets({"a": object_data()})
    destination.client.corrupt_upload = True
    with pytest.raises(migration.MigrationError, match="SHA-256"):
        migration.migrate(source, destination, "copy")
    assert destination.client.reads == 1


def test_verify_missing_is_read_only_and_extra_keys_are_never_deleted():
    source, destination = buckets({"a": object_data()})
    with pytest.raises(migration.MigrationError, match="Faltan"):
        migration.migrate(source, destination, "verify")
    destination.client.objects["unrelated"] = object_data()
    with pytest.raises(migration.MigrationError, match="ajenas"):
        migration.migrate(source, destination, "copy")
    assert "unrelated" in destination.client.objects
    assert source.client.writes == destination.client.writes == 0


def test_concurrent_source_change_requires_another_pass(monkeypatch):
    source, destination = buckets({"a": object_data()})
    initial = source.inventory()
    monkeypatch.setattr(source, "inventory", Mock(side_effect=[initial, {}]))
    with pytest.raises(migration.MigrationError, match="Cambió"):
        migration.migrate(source, destination, "copy")


def test_same_bucket_is_rejected():
    source, _ = buckets()
    with pytest.raises(migration.MigrationError, match="distintos"):
        migration.migrate(source, source)


def test_cli_keeps_secrets_out_of_arguments_and_output(monkeypatch, capsys):
    credentials = {
        "endpoint": "https://s3.example.test", "region": "auto", "urlStyle": "virtual-host",
        "accessKeyId": "access-private", "secretAccessKey": "secret-private",
    }
    payload = {"data": {label: [{**credentials, "bucketName": label}]
                        for label in ("source", "destination")}}
    run = Mock(return_value=subprocess.CompletedProcess([], 0, json.dumps(payload), ""))
    client = Mock()
    monkeypatch.setattr(storage.shutil, "which", lambda _: "railway")
    monkeypatch.setattr(storage.subprocess, "run", run)
    monkeypatch.setattr(storage.boto3, "client", client)
    ref = migration.BucketReference("project", "environment", "bucket")
    source, destination = migration.load_buckets(ref, ref)
    assert source.name == "source" and destination.name == "destination"
    assert run.call_args.args[0] == ["railway", "api", "--compact"]
    assert run.call_args.kwargs["input"].startswith("query {")
    assert client.call_args.kwargs["config"].s3["addressing_style"] == "virtual"
    assert capsys.readouterr().out == ""
    assert credentials["secretAccessKey"] not in repr(run.call_args)


def test_s3_errors_are_sanitized(monkeypatch, capsys):
    source, destination = buckets({"a": object_data()})
    error = ClientError({"Error": {"Code": "Denied", "Message": "secret-private"}}, "GetObject")
    monkeypatch.setattr(source.client, "get_object", Mock(side_effect=error))
    with pytest.raises(migration.MigrationError, match="ClientError") as result:
        migration.migrate(source, destination, "copy")
    assert "secret-private" not in str(result.value)
    assert "secret-private" not in capsys.readouterr().out


def test_failed_conditional_upload_never_retries_with_overwrite(monkeypatch):
    source, destination = buckets({"a": object_data()})
    error = ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
    put = Mock(side_effect=error)
    monkeypatch.setattr(destination.client, "put_object", put)
    with pytest.raises(migration.MigrationError, match="ClientError"):
        migration.migrate(source, destination, "copy")
    assert put.call_count == 1
    assert put.call_args.kwargs["IfNoneMatch"] == "*"
    assert source.client.objects["a"] == object_data()


def test_oversized_object_is_rejected_before_any_copy(monkeypatch):
    source, destination = buckets({"a": object_data()})
    monkeypatch.setattr(migration, "MAX_OBJECT_SIZE", 1)
    with pytest.raises(migration.MigrationError, match="5 GiB"):
        migration.migrate(source, destination, "copy")
    assert source.client.reads == 0
    assert destination.client.writes == 0


def test_main_never_prints_railway_error_details(monkeypatch, capsys):
    arguments = {f"{side}_{resource}": "resource-id"
                 for side in ("source", "destination") for resource in ("project", "environment", "bucket")}
    monkeypatch.setattr(
        migration, "parse_args", lambda: migration.argparse.Namespace(**arguments, mode="plan", workers=8),
    )
    error = subprocess.CalledProcessError(1, "railway", output="access-private", stderr="secret-private")
    monkeypatch.setattr(migration, "load_buckets", Mock(side_effect=error))
    assert migration.main() == 1
    output = capsys.readouterr()
    assert "CalledProcessError" in output.err
    assert "access-private" not in output.out + output.err
    assert "secret-private" not in output.out + output.err


def test_parallel_failure_does_not_schedule_later_batches():
    source, destination = buckets({
        "a": object_data(Metadata={"sha256": "bad"}),
        "b": object_data(),
        "c-later-batch": object_data(),
    })
    with pytest.raises(migration.MigrationError, match="SHA-256"):
        migration.migrate(source, destination, "copy", workers=2)
    assert "c-later-batch" not in destination.client.objects


@pytest.mark.parametrize("workers", [0, 17])
def test_invalid_parallelism_is_rejected(workers):
    source, destination = buckets()
    with pytest.raises(migration.MigrationError, match="paralelas"):
        migration.migrate(source, destination, "copy", workers=workers)

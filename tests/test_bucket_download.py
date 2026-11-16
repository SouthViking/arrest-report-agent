"""Local snapshots retain S3 identities and reject incomplete or corrupted backups."""

from __future__ import annotations

import importlib
import json
import sys
from datetime import UTC, datetime
from unittest.mock import Mock

import pytest
from test_bucket_copy import buckets, object_data, storage

backup = importlib.import_module("download_bucket")
REFERENCE = storage.BucketReference("project", "environment", "bucket")


def test_plan_lists_without_downloading_or_creating_destination(tmp_path):
    source, _ = buckets({"a.png": object_data()})
    root = tmp_path / "snapshot"
    backup.download(source, REFERENCE, root, plan=True)
    assert not root.exists()
    assert source.client.reads == source.client.writes == 0


def test_snapshot_preserves_content_metadata_tags_and_arbitrary_keys(tmp_path):
    objects = {
        "../outside.png": object_data(CacheControl="immutable", tags={"case": "robo & recuperación"}),
        "/absolute.JPG": object_data(b"another", Expires=datetime(2030, 1, 1, tzinfo=UTC)),
        "C:\\windows\\CON.png": object_data(b"windows"),
        "folder/image.png": object_data(b""),
    }
    source, _ = buckets(objects)
    root = tmp_path / "snapshot"
    backup.download(source, REFERENCE, root, workers=2)
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "complete"
    assert manifest["object_count"] == 4
    assert {entry["key"] for entry in manifest["objects"]} == set(objects)
    for entry in manifest["objects"]:
        assert (root / entry["file"]).read_bytes() == objects[entry["key"]]["body"]
        assert (root / entry["file"]).resolve().is_relative_to(root)
        assert entry["tags"] == objects[entry["key"]]["tags"]
        assert entry["properties"]["Metadata"] == objects[entry["key"]]["Metadata"]
    assert not (tmp_path / "outside.png").exists()
    assert source.client.writes == 0
    assert "secretAccessKey" not in (root / "manifest.json").read_text(encoding="utf-8")
    backup.verify(root)


def test_local_verification_needs_neither_cli_nor_source_credentials(tmp_path, monkeypatch, capsys):
    source, _ = buckets({"image.png": object_data()})
    root = tmp_path / "snapshot"
    backup.download(source, REFERENCE, root)
    monkeypatch.setattr(sys, "argv", ["download_bucket.py", "--mode", "verify", "--destination", str(root)])
    loader = Mock(side_effect=AssertionError("Must not query Railway"))
    monkeypatch.setattr(backup, "load_buckets", loader)
    assert backup.main() == 0
    loader.assert_not_called()
    assert "local verificado" in capsys.readouterr().out


def test_local_content_corruption_is_detected(tmp_path):
    source, _ = buckets({"a": object_data()})
    root = tmp_path / "snapshot"
    manifest = backup.download(source, REFERENCE, root)
    path = root / manifest["objects"][0]["file"]
    path.write_bytes(b"other")
    with pytest.raises(storage.BucketError, match="SHA-256"):
        backup.verify(root)


def test_nonempty_destination_and_existing_objects_are_never_overwritten(tmp_path):
    source, _ = buckets({"a": object_data()})
    sentinel = tmp_path / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")
    with pytest.raises(storage.BucketError, match="nueva o vacía"):
        backup.download(source, REFERENCE, tmp_path)
    assert sentinel.read_text(encoding="utf-8") == "keep"
    path = tmp_path / backup.object_path("a")
    path.parent.mkdir(parents=True)
    path.write_bytes(b"existing")
    with pytest.raises(FileExistsError):
        backup.download_object(source, source.inventory()["a"], tmp_path)
    assert path.read_bytes() == b"existing"


def test_invalid_source_hash_removes_partial_file_and_marks_snapshot_failed(tmp_path):
    source, _ = buckets({"a": object_data(Metadata={"sha256": "bad"})})
    root = tmp_path / "snapshot"
    with pytest.raises(storage.BucketError, match="SHA-256"):
        backup.download(source, REFERENCE, root)
    assert not (root / backup.object_path("a")).exists()
    assert json.loads((root / "manifest.json").read_text())["status"] == "failed"
    with pytest.raises(storage.BucketError, match="completo"):
        backup.verify(root)


def test_source_inventory_change_prevents_success(tmp_path, monkeypatch):
    source, _ = buckets({"a": object_data()})
    monkeypatch.setattr(source, "inventory", Mock(side_effect=[source.inventory(), {}]))
    root = tmp_path / "snapshot"
    with pytest.raises(storage.BucketError, match="cambió"):
        backup.download(source, REFERENCE, root)
    assert json.loads((root / "manifest.json").read_text())["status"] == "failed"


def test_verify_rejects_unsafe_manifest_paths_before_reading(tmp_path):
    source, _ = buckets({"a": object_data()})
    root = tmp_path / "snapshot"
    backup.download(source, REFERENCE, root)
    path = root / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["objects"][0]["file"] = "../outside"
    path.write_text(json.dumps(manifest))
    with pytest.raises(storage.BucketError, match="inválidas"):
        backup.verify(root)


def test_prefix_is_forwarded_to_the_remote_inventory(tmp_path, monkeypatch):
    source, _ = buckets()
    inventory = Mock(return_value={})
    monkeypatch.setattr(source, "inventory", inventory)
    backup.download(source, REFERENCE, tmp_path / "snapshot", prefix="suspects/")
    assert inventory.call_count == 2
    assert all(call.args == ("suspects/",) for call in inventory.call_args_list)


def test_snapshot_inside_repository_is_rejected_before_any_download(monkeypatch):
    source, _ = buckets()
    inventory = Mock(side_effect=AssertionError("Must reject path first"))
    monkeypatch.setattr(source, "inventory", inventory)
    with pytest.raises(storage.BucketError, match="fuera del repositorio"):
        backup.download(source, REFERENCE, backup.REPOSITORY_ROOT / "backup")


@pytest.mark.parametrize("arguments", [
    [], ["--workers", "17"], ["--project", "not-a-uuid"],
    ["--mode", "verify", "--prefix", "image/"],
])
def test_cli_validates_source_ids_workers_and_offline_mode(tmp_path, monkeypatch, arguments):
    monkeypatch.setattr(sys, "argv", ["download_bucket.py", "--destination", str(tmp_path), *arguments])
    with pytest.raises(SystemExit) as result:
        backup.parse_args()
    assert result.value.code == 2


def test_single_bucket_credentials_do_not_require_a_destination(monkeypatch):
    data = {"data": {"source": [{
        "bucketName": "only-source", "endpoint": "https://s3.example.test", "region": "auto",
        "urlStyle": "path", "accessKeyId": "access", "secretAccessKey": "secret",
    }]}}
    run = Mock(return_value=storage.subprocess.CompletedProcess([], 0, json.dumps(data), ""))
    monkeypatch.setattr(storage.shutil, "which", lambda _: "railway")
    monkeypatch.setattr(storage.subprocess, "run", run)
    client = Mock()
    monkeypatch.setattr(storage.boto3, "client", client)
    bucket, = storage.load_buckets(REFERENCE)
    assert bucket.name == "only-source"
    assert "destination:" not in run.call_args.kwargs["input"]
    assert client.call_args.kwargs["config"].s3["addressing_style"] == "path"


def test_cli_error_messages_never_expose_raw_sdk_details(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        sys, "argv", ["download_bucket.py", "--mode", "verify", "--destination", str(tmp_path)],
    )
    monkeypatch.setattr(backup, "verify", Mock(side_effect=OSError("secret-value")))
    assert backup.main() == 1
    assert "secret-value" not in capsys.readouterr().err


@pytest.mark.parametrize("manifest", [
    None, [], {"schema_version": 1, "status": "complete", "objects": [None]},
])
def test_malformed_manifest_is_rejected_with_a_controlled_error(tmp_path, manifest):
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(storage.BucketError, match="manifiesto"):
        backup.verify(tmp_path)

"""Download a Railway bucket snapshot, or verify a local snapshot without network access."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from itertools import batched
from pathlib import Path, PurePosixPath
from tempfile import NamedTemporaryFile
from typing import Any
from uuid import UUID

from botocore.exceptions import BotoCoreError, ClientError
from bucket_storage import Bucket, BucketError, BucketReference, ObjectVersion, load_buckets, read_object

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_VERSION = 1


def destination_path(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if resolved.is_relative_to(REPOSITORY_ROOT):
        raise BucketError("El respaldo debe quedar fuera del repositorio.")
    return resolved


def object_path(key: str) -> str:
    """Map arbitrary S3 keys to portable, collision-resistant local names."""
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    suffix = PurePosixPath(key).suffix
    if not re.fullmatch(r"\.[A-Za-z0-9]{1,12}", suffix):
        suffix = ""
    return f"objects/{digest[:2]}/{digest}{suffix}"


def json_value(value: Any) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    raise TypeError("Unsupported manifest value")


def save_manifest(root: Path, manifest: dict[str, Any]) -> None:
    temporary_path = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=root, delete=False) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(manifest, temporary, ensure_ascii=False, indent=2, default=json_value)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        temporary_path.replace(root / "manifest.json")
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def download_object(bucket: Bucket, version: ObjectVersion, root: Path) -> dict[str, Any]:
    relative = object_path(version.key)
    path = root / relative
    created = False
    try:
        tags = bucket.tags(version.key)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.parent.resolve().is_relative_to(root):
            raise BucketError("La ruta local del objeto sale del directorio de respaldo.")
        with path.open("xb") as stream:
            created = True
            digest, attrs = read_object(bucket, version, stream)
            stream.flush()
            os.fsync(stream.fileno())
        with path.open("rb") as stream:
            local_digest = hashlib.file_digest(stream, "sha256").hexdigest()
        if local_digest != digest or path.stat().st_size != version.size:
            raise BucketError("El archivo local no coincide con el objeto descargado.")
        return {
            "key": version.key, "file": relative, "size_bytes": version.size, "sha256": digest,
            "etag": version.etag, "last_modified": version.modified, "properties": attrs, "tags": tags,
        }
    except Exception:
        if created:
            path.unlink(missing_ok=True)
        raise


def download(
    bucket: Bucket, reference: BucketReference, destination: Path, *, prefix: str = "", workers: int = 8,
    plan: bool = False,
) -> dict[str, Any] | None:
    if not 1 <= workers <= 16:
        raise BucketError("El número de descargas paralelas debe estar entre 1 y 16.")
    root = destination_path(destination)
    before = bucket.inventory(prefix)
    count, total_bytes = len(before), sum(item.size for item in before.values())
    print(f"Origen: {count} objetos, {total_bytes} bytes.", flush=True)
    if plan:
        print("Vista previa: no se descargó contenido ni se creó el directorio local.")
        return None
    if root.exists() and any(root.iterdir()):
        raise BucketError("El destino debe ser una carpeta nueva o vacía.")
    root.mkdir(parents=True, exist_ok=True)
    # Exclusive marker also prevents a second invocation from writing into the same snapshot.
    with (root / "manifest.json").open("x", encoding="utf-8") as stream:
        stream.write('{"schema_version": 1, "status": "in_progress"}\n')
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION, "status": "in_progress", "created_at": datetime.now(UTC),
        "source": {
            "project_id": reference.project_id, "environment_id": reference.environment_id,
            "bucket_id": reference.bucket_id, "bucket_name": bucket.name, "prefix": prefix,
        },
        "object_count": count, "total_bytes": total_bytes, "objects": [],
    }
    entries = manifest["objects"]
    try:
        save_manifest(root, manifest)
        with ThreadPoolExecutor(max_workers=workers) as executor:
            for batch in batched(sorted(before.values(), key=lambda item: item.key), workers):
                pending = [executor.submit(download_object, bucket, item, root) for item in batch]
                for future in pending:
                    entries.append(future.result())
                save_manifest(root, manifest)
                print(f"Descargados y verificados: {len(entries)}/{count}.", flush=True)
        if bucket.inventory(prefix) != before:
            raise BucketError("El bucket cambió durante la descarga. Repite con las escrituras detenidas.")
        manifest["status"] = "complete"
        manifest["completed_at"] = datetime.now(UTC)
        save_manifest(root, manifest)
        print(f"Respaldo completo: {count} objetos y {total_bytes} bytes verificados.")
        return manifest
    except Exception:
        manifest["status"] = "failed"
        save_manifest(root, manifest)
        raise


def verify(destination: Path) -> None:
    root = destination_path(destination)
    with (root / "manifest.json").open(encoding="utf-8") as stream:
        manifest = json.load(stream)
    if (not isinstance(manifest, dict) or manifest.get("schema_version") != SCHEMA_VERSION
            or manifest.get("status") != "complete" or not isinstance(manifest.get("objects"), list)):
        raise BucketError("El manifiesto no corresponde a un respaldo completo compatible.")
    entries = manifest["objects"]
    keys = set()
    total_bytes = 0
    for entry in entries:
        if (not isinstance(entry, dict) or not all(isinstance(entry.get(name), str)
                                                 for name in ("key", "file", "sha256"))
                or not isinstance(entry.get("properties"), dict)
                or not isinstance(entry["properties"].get("Metadata", {}), dict)):
            raise BucketError("El manifiesto contiene un objeto inválido.")
        key, relative = entry["key"], entry["file"]
        path = root / relative
        if key in keys or relative != object_path(key) or not path.resolve().is_relative_to(root):
            raise BucketError("El manifiesto contiene claves duplicadas o rutas locales inválidas.")
        keys.add(key)
        if path.is_symlink() or not path.is_file():
            raise BucketError("Falta un archivo regular del respaldo.")
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        size = path.stat().st_size
        declared = entry["properties"].get("Metadata", {}).get("sha256")
        if (size != entry["size_bytes"] or digest != entry["sha256"]
                or (declared and (not isinstance(declared, str) or declared.lower() != digest))):
            raise BucketError("Un archivo no coincide con el tamaño o SHA-256 del manifiesto.")
        total_bytes += size
    if len(entries) != manifest["object_count"] or total_bytes != manifest["total_bytes"]:
        raise BucketError("Las cantidades del manifiesto no coinciden con sus objetos.")
    print(f"Respaldo local verificado: {len(entries)} objetos y {total_bytes} bytes.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("plan", "download", "verify"), default="download")
    for resource in ("project", "environment", "bucket"):
        parser.add_argument(f"--{resource}", type=UUID, metavar="UUID")
    parser.add_argument("--destination", type=Path, required=True, help="Carpeta fuera del repositorio.")
    parser.add_argument("--prefix", default="", help="Prefijo S3 literal; vacío descarga todo el bucket.")
    parser.add_argument("--workers", type=int, choices=range(1, 17), default=8, metavar="1-16")
    args = parser.parse_args()
    if args.mode != "verify" and any(getattr(args, resource) is None
                                     for resource in ("project", "environment", "bucket")):
        parser.error("--project, --environment y --bucket son obligatorios para plan/download.")
    if args.mode == "verify" and (args.prefix or any(getattr(args, resource) is not None
                                                   for resource in ("project", "environment", "bucket"))):
        parser.error("verify solo utiliza --destination; no acepta origen ni prefijo.")
    return args


def main() -> int:
    args = parse_args()
    try:
        if args.mode == "verify":
            verify(args.destination)
        else:
            reference = BucketReference(str(args.project), str(args.environment), str(args.bucket))
            bucket, = load_buckets(reference)
            try:
                download(bucket, reference, args.destination, prefix=args.prefix, workers=args.workers,
                         plan=args.mode == "plan")
            finally:
                bucket.client.close()
    except (BucketError, ClientError, BotoCoreError, OSError, subprocess.SubprocessError,
            ValueError, KeyError, TypeError) as error:
        message = (str(error) if isinstance(error, BucketError)
                   else f"Respaldo fallido ({type(error).__name__}).")
        print(message, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

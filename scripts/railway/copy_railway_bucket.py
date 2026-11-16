"""Copy and verify Railway buckets without changing the bot configuration.

The default mode only lists objects. Credentials come from the authenticated
Railway CLI, stay in memory and are never printed or passed as command arguments.
"""

from __future__ import annotations

import argparse
import hashlib
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from itertools import batched
from tempfile import TemporaryFile
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

from botocore.exceptions import BotoCoreError, ClientError
from bucket_storage import Bucket, BucketReference, ObjectVersion, load_buckets, read_object
from bucket_storage import BucketError as MigrationError

MAX_OBJECT_SIZE = 5 * 1024**3  # Single PUT; attachments are much smaller.


def verify_object(
    bucket: Bucket, version: ObjectVersion, fingerprint: str, expected_properties: dict[str, Any],
    expected_tags: dict[str, str],
) -> None:
    actual_hash, actual_properties = read_object(bucket, version)
    if (actual_hash != fingerprint or actual_properties != expected_properties
            or bucket.tags(version.key) != expected_tags):
        raise MigrationError("El contenido o los metadatos difieren. No se sobrescribirá el objeto.")


def transfer_object(
    source: Bucket, destination: Bucket, version: ObjectVersion, existing: ObjectVersion | None,
) -> tuple[ObjectVersion, bool]:
    key = version.key
    try:
        tags = source.tags(key)
        if existing is not None:
            fingerprint, attrs = read_object(source, version)
            verify_object(destination, existing, fingerprint, attrs, tags)
            return existing, False
        # One temporary file per worker, outside the repository; removed on close.
        with TemporaryFile() as temporary:
            fingerprint, attrs = read_object(source, version, temporary)
            temporary.seek(0)
            arguments = dict(attrs)
            if tags:
                arguments["Tagging"] = urlencode(sorted(tags.items()))
            destination.client.put_object(
                Bucket=destination.name, Key=key, Body=temporary,
                ContentLength=version.size, IfNoneMatch="*", **arguments,
            )
        head = destination.client.head_object(Bucket=destination.name, Key=key)
        uploaded = ObjectVersion(key, head["ContentLength"], head["ETag"], head["LastModified"])
        verify_object(destination, uploaded, fingerprint, attrs, tags)
        return uploaded, True
    except (MigrationError, ClientError, BotoCoreError, OSError) as error:
        identifier = hashlib.sha256(key.encode()).hexdigest()[:12]
        detail = str(error) if isinstance(error, MigrationError) else type(error).__name__
        raise MigrationError(f"Objeto (referencia {identifier}): {detail}") from None


def migrate(source: Bucket, destination: Bucket, mode: str = "plan", workers: int = 8) -> None:
    if mode not in {"plan", "copy", "verify"}:
        raise MigrationError("Modo inválido.")
    if not 1 <= workers <= 16:
        raise MigrationError("El número de transferencias paralelas debe estar entre 1 y 16.")
    if source.name == destination.name:
        raise MigrationError("El origen y el destino deben ser buckets distintos.")
    before = source.inventory()
    target = destination.inventory()
    missing = before.keys() - target.keys()
    extra = target.keys() - before.keys()
    total_bytes = sum(item.size for item in before.values())
    print(f"Origen: {len(before)} objetos, {total_bytes} bytes.", flush=True)
    print(f"Destino: {len(target)} objetos; faltan {len(missing)}, adicionales {len(extra)}.", flush=True)
    if extra:
        raise MigrationError("El destino contiene claves ajenas al origen. No se borrará nada.")
    if mode == "plan":
        print("Vista previa: no se descargó contenido ni se escribió en los buckets.")
        return
    if mode == "verify" and missing:
        raise MigrationError("Faltan objetos en el destino. Ejecuta la copia antes de verificar.")
    if mode == "copy" and any(item.size > MAX_OBJECT_SIZE for item in before.values()):
        raise MigrationError("Hay un objeto mayor a 5 GiB; esta herramienta no realiza cargas multipartes.")

    copied = 0
    index = 0
    with ThreadPoolExecutor(max_workers=workers) as executor:
        # Only one bounded batch is scheduled at a time. On error, later batches never start.
        for batch in batched(sorted(before.values(), key=lambda item: item.key), workers):
            pending = [executor.submit(transfer_object, source, destination, item, target.get(item.key))
                       for item in batch]
            for future in pending:
                result, was_copied = future.result()
                target[result.key] = result
                copied += int(was_copied)
                index += 1
            print(f"Verificados: {index}/{len(before)}; copiados: {copied}.", flush=True)

    # Inventories are not atomic snapshots. Detect writes during the run and require another pass.
    if source.inventory() != before or destination.inventory() != target:
        raise MigrationError("Cambió un bucket durante la operación. Repite con las escrituras detenidas.")
    print(f"Correcto: {len(before)} objetos y {total_bytes} bytes verificados; {copied} copiados.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    for side in ("source", "destination"):
        for resource in ("project", "environment", "bucket"):
            parser.add_argument(f"--{side}-{resource}", type=UUID, required=True, metavar="UUID")
    parser.add_argument("--mode", choices=("plan", "copy", "verify"), default="plan")
    parser.add_argument("--workers", type=int, choices=range(1, 17), default=8, metavar="1-16")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    references = [
        BucketReference(*(str(getattr(args, f"{side}_{resource}"))
                          for resource in ("project", "environment", "bucket")))
        for side in ("source", "destination")
    ]
    try:
        source, destination = load_buckets(*references)
        try:
            migrate(source, destination, args.mode, args.workers)
        finally:
            source.client.close()
            destination.client.close()
    except (MigrationError, ClientError, BotoCoreError, OSError, subprocess.SubprocessError,
            ValueError, KeyError, TypeError) as error:
        # Never print raw Railway/S3 exceptions, captured output, or credential dictionaries.
        message = (str(error) if isinstance(error, MigrationError)
                   else f"Operación fallida ({type(error).__name__}).")
        print(message, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

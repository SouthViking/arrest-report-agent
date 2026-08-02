"""Dependency-free HTTP health endpoint for container probes."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable

HealthCheck = Callable[[], Awaitable[dict[str, object]]]


async def serve_health(host: str, port: int, check: HealthCheck) -> asyncio.Server:
    """Start a small HTTP/1.1 server exposing `/health`."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await reader.readline()
            parts = request_line.decode("ascii", errors="ignore").split()
            path = parts[1] if len(parts) >= 2 else "/"
            if path == "/health":
                payload = await check()
                status = "200 OK" if payload.get("ok") else "503 Service Unavailable"
            else:
                payload = {"ok": False, "error": "not_found"}
                status = "404 Not Found"
            body = json.dumps(payload, separators=(",", ":")).encode()
            headers = (
                f"HTTP/1.1 {status}\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n"
                "Connection: close\r\n\r\n"
            )
            writer.write(headers.encode() + body)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    return await asyncio.start_server(handle, host, port)


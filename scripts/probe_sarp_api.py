"""Comprueba el acceso por token a un endpoint documentado de la API de SARP."""

from __future__ import annotations

import os
import sys

import requests

API_URL = "https://api.gta-rol.com/api/police/fines"


def main() -> int:
    token = os.getenv("SARP_API_TOKEN", "").strip()
    if not token:
        print("Falta la variable de entorno SARP_API_TOKEN.", file=sys.stderr)
        return 2

    try:
        response = requests.get(
            API_URL,
            params={"page": 1, "limit": 25, "is_paid": 0},
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
            timeout=15,
        )
    except requests.RequestException as error:
        print(f"La solicitud no pudo completarse: {error}", file=sys.stderr)
        return 1

    print(f"HTTP {response.status_code}")
    print(f"Content-Type: {response.headers.get('content-type', 'desconocido')}")

    if response.headers.get("cf-mitigated") == "challenge":
        print("Cloudflare interceptó la solicitud con un challenge.")
        return 1

    try:
        body = response.json()
    except requests.exceptions.JSONDecodeError:
        print("La respuesta no es JSON.")
        print(response.text[:500])
        return 1

    if not response.ok:
        print(f"Error de la API: {body}")
        return 1

    payload = body.get("payload", {})
    print(
        "Consulta correcta: "
        f"{payload.get('total', 0)} multas, "
        f"{len(payload.get('items', []))} resultados en esta página."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

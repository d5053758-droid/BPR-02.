#!/usr/bin/env bash
# Verify OTCharts credentials with GET /v1/usage, which does not consume data quota.
# The key is entered silently, is never printed, and is removed on exit.
set -euo pipefail

read -r -s -p "Pegá tu clave NUEVA de OTCharts (no se mostrará): " OTCHARTS_API_KEY
printf '\n'
if [[ -z "$OTCHARTS_API_KEY" ]]; then
  printf 'ERROR: no se ingresó ninguna clave. No se hizo ninguna solicitud.\n' >&2
  exit 2
fi
export OTCHARTS_API_KEY
trap 'unset OTCHARTS_API_KEY' EXIT

python - <<'PY'
import json
import os
import sys
import urllib.error
import urllib.request

key = os.environ.get("OTCHARTS_API_KEY", "").strip()
request = urllib.request.Request(
    "https://otcharts.com/v1/usage",
    headers={
        "Authorization": f"Bearer {key}",
        "Accept": "application/json",
        "User-Agent": "BPR-02-auth-check/1.0",
    },
    method="GET",
)
try:
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))
        status = response.status
except urllib.error.HTTPError as exc:
    print(f"Comprobación de autenticación: HTTP {exc.code}")
    try:
        body = json.loads(exc.read().decode("utf-8"))
        detail = body.get("error") or body.get("message")
        if detail:
            print(f"Detalle del proveedor: {detail}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        print("El proveedor no devolvió un error JSON legible.")
    if exc.code == 401:
        print("Revisá que el correo esté confirmado y que la clave sea la nueva, completa y activa.")
        print("Una clave revocada/perdida no se puede recuperar; creá otra desde la cuenta.")
    elif exc.code == 403:
        print("La clave llegó al servidor, pero puede no tener acceso al libro/instrumento solicitado.")
    sys.exit(2)
except urllib.error.URLError as exc:
    print(f"No se pudo conectar con OTCharts: {exc.reason}")
    sys.exit(2)
except (UnicodeDecodeError, json.JSONDecodeError):
    print("OTCharts respondió, pero la respuesta no fue JSON válido.")
    sys.exit(2)

if not isinstance(payload, dict):
    print("OTCharts devolvió un formato inesperado.")
    sys.exit(2)

print(f"Autenticación: OK (HTTP {status})")
print("Plan:", payload.get("planName") or payload.get("plan") or "no indicado")
books = payload.get("books")
if isinstance(books, list):
    print("Libros habilitados:", ", ".join(str(book) for book in books) or "ninguno indicado")
requests = payload.get("requests")
if isinstance(requests, dict):
    print("Solicitudes usadas:", requests.get("used", "no indicado"))
    print("Solicitudes restantes:", requests.get("remaining", "no indicado"))
    print("Período de cuota:", requests.get("per", "no indicado"))
streams = payload.get("streams")
if isinstance(streams, dict):
    print("Streams disponibles en el plan:", streams.get("limit", "no indicado"))
print("Esta consulta es de diagnóstico; no se envían órdenes de trading.")
PY

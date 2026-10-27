#!/usr/bin/env bash
# Single-command setup: install BPR-02, verify OTCharts without spending data
# quota, fetch one recent candle page if needed, assess data quality and start
# the local read-only API. Never submits trading orders.
set -euo pipefail

CANDLE_CSV="${BPR02_CANDLES_OUTPUT:-otc_candles_5s.csv}"
FETCH_REPORT="${BPR02_FETCH_REPORT_OUTPUT:-otc_candles_5s_report.json}"
READINESS_REPORT="${BPR02_READINESS_OUTPUT:-otc_candles_5s_readiness_$(date -u +%Y%m%dT%H%M%SZ).json}"

printf '\nBPR-02: instalación y API local de solo lectura\n'
printf 'No se enviarán operaciones de trading.\n\n'

python -m pip install -e ".[test]"

if [[ -f "$CANDLE_CSV" ]]; then
  printf '\nYa existe el CSV %s; no volveré a descargarlo ni consumir otra solicitud.\n' "$CANDLE_CSV"
else
  if [[ -e "$FETCH_REPORT" ]]; then
    printf 'ERROR: existe %s, pero no existe %s. Para no sobrescribir archivos, renombrá el informe y volvé a ejecutar.\n' "$FETCH_REPORT" "$CANDLE_CSV" >&2
    exit 2
  fi

  read -r -s -p 'Pegá tu clave NUEVA de OTCharts (la entrada queda oculta): ' OTCHARTS_API_KEY
  printf '\n'
  if [[ -z "$OTCHARTS_API_KEY" ]]; then
    printf 'ERROR: no ingresaste una clave; no se solicitó ningún dato.\n' >&2
    exit 2
  fi
  export OTCHARTS_API_KEY
  trap 'unset OTCHARTS_API_KEY' EXIT

  # This diagnostic endpoint does not consume the data quota. Do not print the key.
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
    print(f"Autenticación OTCharts: HTTP {exc.code}", file=sys.stderr)
    try:
        body = json.loads(exc.read().decode("utf-8"))
        detail = body.get("error") or body.get("message")
        if detail:
            print(f"Detalle del proveedor: {detail}", file=sys.stderr)
    except (UnicodeDecodeError, json.JSONDecodeError):
        pass
    if exc.code == 401:
        print("La clave no se reconoció. Revisá que el correo esté confirmado y generá una clave nueva si hace falta.", file=sys.stderr)
    elif exc.code == 403:
        print("La autenticación o el acceso al recurso fue rechazado; revisá el acceso al libro OTC.", file=sys.stderr)
    elif exc.code == 429:
        print("El proveedor indicó que se alcanzó un límite. No volveré a solicitar datos automáticamente.", file=sys.stderr)
    sys.exit(2)
except urllib.error.URLError as exc:
    print(f"No se pudo conectar con OTCharts: {exc.reason}", file=sys.stderr)
    sys.exit(2)
except (UnicodeDecodeError, json.JSONDecodeError):
    print("OTCharts devolvió una respuesta que no es JSON válido.", file=sys.stderr)
    sys.exit(2)

if not isinstance(payload, dict):
    print("OTCharts devolvió un formato inesperado.", file=sys.stderr)
    sys.exit(2)

print(f"Autenticación OTCharts: correcta (HTTP {status})")
print("Plan:", payload.get("planName") or payload.get("plan") or "no indicado")
books = payload.get("books")
if isinstance(books, list):
    print("Libros habilitados:", ", ".join(str(value) for value in books) or "ninguno indicado")
requests = payload.get("requests")
if isinstance(requests, dict):
    print("Solicitudes usadas:", requests.get("used", "no indicado"))
    print("Solicitudes restantes:", requests.get("remaining", "no indicado"))
    print("Período de cuota:", requests.get("per", "no indicado"))
streams = payload.get("streams")
if isinstance(streams, dict):
    print("Streams permitidos:", streams.get("limit", "no indicado"))
PY

  printf '\nSolicitando una sola página de hasta 1.500 velas de 5 segundos...\n'
  bpr02-otc-data candles \
    --symbol EURUSD_otc \
    --timeframe-seconds 5 \
    --limit 1500 \
    --output "$CANDLE_CSV" \
    --report-output "$FETCH_REPORT"

  unset OTCHARTS_API_KEY
  trap - EXIT
fi

printf '\nEvaluando estructura y continuidad temporal...\n'
if [[ -f "$FETCH_REPORT" ]]; then
  bpr02-readiness "$CANDLE_CSV" \
    --provider-report "$FETCH_REPORT" \
    --expected-symbol EURUSD_otc \
    --expected-interval-seconds 5 \
    --lookback 12 \
    --expiry-bars 1 \
    --max-missing-bars 0 \
    --output "$READINESS_REPORT"
else
  printf 'Aviso: no hay informe del proveedor; evaluando solamente el CSV existente.\n'
  bpr02-readiness "$CANDLE_CSV" \
    --expected-symbol EURUSD_otc \
    --expected-interval-seconds 5 \
    --lookback 12 \
    --expiry-bars 1 \
    --max-missing-bars 0 \
    --output "$READINESS_REPORT"
fi

printf '\nDatos e informe de calidad listos. Iniciando API local.\n'
printf 'Desde esta terminal, detener el servidor con Ctrl+C.\n\n'
exec bpr02-api

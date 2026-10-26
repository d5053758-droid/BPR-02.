#!/usr/bin/env bash
# One-request, read-only starter workflow for BPR-02.
# The API key is read silently and remains only in this child shell's environment.
set -euo pipefail

for command_name in bpr02-otc-data bpr02-readiness; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    printf 'ERROR: %s was not found. From the repository root run: python -m pip install -e ".[test]"\n' "$command_name" >&2
    exit 2
  fi
done

CANDLE_CSV="${BPR02_CANDLES_OUTPUT:-otc_candles_5s.csv}"
FETCH_REPORT="${BPR02_FETCH_REPORT_OUTPUT:-otc_candles_5s_report.json}"
READINESS_REPORT="${BPR02_READINESS_OUTPUT:-otc_candles_5s_readiness.json}"

if [[ "$CANDLE_CSV" == "$FETCH_REPORT" || "$CANDLE_CSV" == "$READINESS_REPORT" || "$FETCH_REPORT" == "$READINESS_REPORT" ]]; then
  printf 'ERROR: output paths must be different files.\n' >&2
  exit 2
fi

for output_path in "$CANDLE_CSV" "$FETCH_REPORT" "$READINESS_REPORT"; do
  if [[ -e "$output_path" ]]; then
    printf 'ERROR: refusing to overwrite existing file: %s\nRename or move it, or set a different BPR02_*_OUTPUT path.\n' "$output_path" >&2
    exit 2
  fi
done

printf 'This performs one historical candle request; it does not stream or place orders.\n'
read -r -s -p 'OTCharts API key (input hidden; never paste it into chat): ' OTCHARTS_API_KEY
printf '\n'
if [[ -z "$OTCHARTS_API_KEY" ]]; then
  printf 'ERROR: empty API key; no request was made.\n' >&2
  exit 2
fi
export OTCHARTS_API_KEY
trap 'unset OTCHARTS_API_KEY' EXIT

bpr02-otc-data candles \
  --symbol EURUSD_otc \
  --timeframe-seconds 5 \
  --limit 1500 \
  --output "$CANDLE_CSV" \
  --report-output "$FETCH_REPORT"

unset OTCHARTS_API_KEY
trap - EXIT

bpr02-readiness "$CANDLE_CSV" \
  --provider-report "$FETCH_REPORT" \
  --expected-symbol EURUSD_otc \
  --expected-interval-seconds 5 \
  --lookback 12 \
  --expiry-bars 1 \
  --max-missing-bars 0 \
  --output "$READINESS_REPORT"

printf '\nFinished. Inspect these files before any strategy test:\n  %s\n  %s\n  %s\n' "$CANDLE_CSV" "$FETCH_REPORT" "$READINESS_REPORT"
printf 'Reminder: a passed readiness report is not evidence of three-second price parity or profitability.\n'

"""Fetch one EUR/USD 1-minute candle from Twelve Data. Never submits orders."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


BASE_DIR = Path(__file__).resolve().parent
OUTPUT_FILE = BASE_DIR / "data" / "quotes.jsonl"
ENDPOINT = "https://api.twelvedata.com/time_series"


def main() -> None:
    api_key = os.environ.get("TWELVEDATA_API_KEY", "").strip()
    if not api_key:
        raise SystemExit(
            "Missing TWELVEDATA_API_KEY. Add it as a private Codespaces secret; "
            "do not put the key in source code or chat."
        )

    params = urlencode({
        "symbol": "EUR/USD",
        "interval": "1min",
        "outputsize": "1",
        "timezone": "UTC",
        "apikey": api_key,
    })
    request = Request(
        f"{ENDPOINT}?{params}",
        headers={"User-Agent": "BPR-02-free-research-reader/1.0"},
    )

    try:
        with urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise SystemExit(
            f"Twelve Data returned HTTP {exc.code}. Check the API key and quota "
            "in the provider dashboard."
        ) from None
    except URLError as exc:
        raise SystemExit(
            f"Could not reach Twelve Data: {exc.reason}. Check the network and retry."
        ) from None
    except (TimeoutError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Timed out or received invalid JSON: {type(exc).__name__}.") from None

    values = payload.get("values")
    if not isinstance(values, list) or not values:
        # Don't echo the full response URL: it can contain the API key.
        message = payload.get("message") or payload.get("status") or "no candle returned"
        raise SystemExit(
            f"Twelve Data did not return candle data ({message}). "
            "Check API key, symbol, plan limits, and provider status."
        )

    candle = values[0]
    try:
        record = {
            "fetched_at_utc": datetime.now(timezone.utc).isoformat(),
            "provider": "Twelve Data",
            "symbol": "EUR/USD",
            "timeframe": "1min",
            "candle_time_utc": candle["datetime"],
            "open": float(candle["open"]),
            "high": float(candle["high"]),
            "low": float(candle["low"]),
            "close": float(candle["close"]),
            "mode": "external_market_data_read_only",
            "fxpro_account_connected": False,
            "orders_submitted": 0,
        }
    except (KeyError, TypeError, ValueError) as exc:
        raise SystemExit(f"Unexpected candle format: {type(exc).__name__}.") from None

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_FILE.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    print(json.dumps(record, ensure_ascii=False, indent=2))
    print(f"Saved locally to {OUTPUT_FILE}")
    print("Safety check: fxpro_account_connected=false; orders_submitted=0")


if __name__ == "__main__":
    main()

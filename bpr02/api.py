"""Read-only local HTTP API for validated BPR-02 market-data files.

The service does not collect prices by itself, connect to Pocket Option, or
submit orders. It serves only datasets already saved locally by an authorized
data adapter. It binds to loopback by default and refuses non-loopback binds
unless an API bearer token is configured.
"""
from __future__ import annotations

import argparse
import csv
import hmac
import ipaddress
import json
import math
import os
import sys
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

DEFAULT_SYMBOL = "EURUSD_otc"
DEFAULT_TIMEFRAME_SECONDS = 5
MAX_CANDLE_LIMIT = 1500


def _configured_path(env_name: str, default: str) -> Path:
    return Path(os.environ.get(env_name, default)).expanduser()


def _symbol() -> str:
    return os.environ.get("BPR02_API_SYMBOL", DEFAULT_SYMBOL).strip() or DEFAULT_SYMBOL


def _timeframe_seconds() -> int:
    raw = os.environ.get("BPR02_API_TIMEFRAME_SECONDS", str(DEFAULT_TIMEFRAME_SECONDS))
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError("BPR02_API_TIMEFRAME_SECONDS must be an integer") from exc
    if value <= 0:
        raise ValueError("BPR02_API_TIMEFRAME_SECONDS must be greater than zero")
    return value


def _normalise_timestamp(value: str) -> tuple[str, float]:
    text = value.strip()
    if not text:
        raise ValueError("timestamp is empty")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid ISO-8601 timestamp {value!r}") from exc
    # The BPR-02 CSV importer treats timestamps without a timezone as UTC.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    return parsed.isoformat().replace("+00:00", "Z"), parsed.timestamp()


def _load_candles() -> list[dict[str, Any]]:
    path = _configured_path("BPR02_API_CANDLES", "otc_candles_5s.csv")
    if not path.is_file():
        raise FileNotFoundError(f"candle dataset not found: {path}")
    candles: list[dict[str, Any]] = []
    seen_timestamps: set[str] = set()
    previous_epoch: float | None = None
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames:
                raise ValueError("candle CSV has no header")
            columns = {name.strip().lower(): name for name in reader.fieldnames if name}
            required = {"timestamp", "open", "high", "low", "close"}
            missing = sorted(required - set(columns))
            if missing:
                raise ValueError(f"candle CSV is missing required columns: {missing}")
            for line_number, row in enumerate(reader, start=2):
                try:
                    timestamp, epoch = _normalise_timestamp(str(row[columns["timestamp"]] or ""))
                    if timestamp in seen_timestamps:
                        raise ValueError(f"duplicate timestamp {timestamp}")
                    if previous_epoch is not None and epoch <= previous_epoch:
                        raise ValueError("timestamps are not in strictly increasing order")
                    prices: dict[str, float] = {}
                    for field in ("open", "high", "low", "close"):
                        raw = row[columns[field]]
                        price = float(str(raw).strip())
                        if not math.isfinite(price) or price <= 0:
                            raise ValueError(f"{field} must be finite and greater than zero")
                        prices[field] = price
                    if prices["high"] < max(prices["open"], prices["close"], prices["low"]):
                        raise ValueError("high is below open, close, or low")
                    if prices["low"] > min(prices["open"], prices["close"], prices["high"]):
                        raise ValueError("low is above open, close, or high")
                except (TypeError, ValueError, OverflowError) as exc:
                    raise ValueError(f"invalid candle CSV row {line_number}: {exc}") from exc
                candles.append({"timestamp": timestamp, **prices})
                seen_timestamps.add(timestamp)
                previous_epoch = epoch
    except UnicodeDecodeError as exc:
        raise ValueError("candle CSV must be UTF-8 text") from exc
    if not candles:
        raise ValueError("candle CSV contains no candle rows")
    return candles


def _read_optional_json(env_name: str, default: str) -> dict[str, Any] | None:
    path = _configured_path(env_name, default)
    if not path.is_file():
        return None
    try:
        with path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read JSON report {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"JSON report {path} must contain an object")
    return payload


def _source_metadata() -> dict[str, Any]:
    report = _read_optional_json("BPR02_API_CANDLES_REPORT", "otc_candles_5s_report.json")
    if not report:
        return {
            "source": "local_csv_file",
            "provider_metadata_available": False,
            "parity_with_pocket_option_confirmed": False,
        }
    return {
        "source": report.get("source", "provider_not_identified"),
        "provider_metadata_available": True,
        "parity_with_pocket_option_confirmed": False,
        "provider_status": report.get("status"),
        "provider_interval": report.get("provider_interval"),
        "provider_count": report.get("provider_count"),
        "instrument_status": report.get("instrument_status"),
    }


def _quality_report() -> dict[str, Any]:
    path = _configured_path("BPR02_API_CANDLES", "otc_candles_5s.csv")
    timeframe = _timeframe_seconds()
    symbol = _symbol()
    if not path.is_file():
        return {
            "status": "data_missing",
            "data_available": False,
            "symbol": symbol,
            "timeframe_seconds": timeframe,
            "dataset_path": str(path),
            "candles": 0,
            "warnings": ["No local candle CSV exists yet; no market data are being invented."],
            "orders_submitted": 0,
        }
    try:
        candles = _load_candles()
    except (OSError, ValueError) as exc:
        return {
            "status": "invalid_data",
            "data_available": False,
            "symbol": symbol,
            "timeframe_seconds": timeframe,
            "dataset_path": str(path),
            "candles": None,
            "error": str(exc),
            "warnings": ["Dataset failed validation and is not served by /v1/candles."],
            "orders_submitted": 0,
        }

    epochs = [_normalise_timestamp(row["timestamp"])[1] for row in candles]
    gaps = [right - left for left, right in zip(epochs, epochs[1:])]
    irregular = [gap for gap in gaps if abs(gap - timeframe) > 1e-6]
    estimated_missing = sum(max(int(round(gap / timeframe)) - 1, 0) for gap in irregular if gap > timeframe * 1.5)
    warnings: list[str] = []
    if irregular:
        warnings.append("Observed timestamp intervals differ from the configured timeframe.")
    if len(candles) < 2:
        warnings.append("At least two candles are needed to assess interval spacing.")
    status = "ok" if not warnings else "warning"
    return {
        "status": status,
        "data_available": True,
        "symbol": symbol,
        "timeframe_seconds": timeframe,
        "dataset_path": str(path),
        "candles": len(candles),
        "first_timestamp": candles[0]["timestamp"],
        "last_timestamp": candles[-1]["timestamp"],
        "span_seconds": epochs[-1] - epochs[0],
        "observed_intervals": len(gaps),
        "irregular_intervals": len(irregular),
        "estimated_missing_candles": estimated_missing,
        "warnings": warnings,
        "source_metadata": _source_metadata(),
        "orders_submitted": 0,
    }


def _latest_tick() -> dict[str, Any]:
    path = _configured_path("BPR02_API_TICKS", "otc_ticks_raw.jsonl")
    if not path.is_file():
        raise FileNotFoundError(f"raw tick log not found: {path}")
    wanted_symbol = _symbol()
    latest: dict[str, Any] | None = None
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON Lines record at line {line_number}") from exc
            if not isinstance(record, dict) or record.get("event") != "tick":
                continue
            payload = record.get("payload")
            if not isinstance(payload, dict) or payload.get("symbol") != wanted_symbol:
                continue
            try:
                price = float(payload["price"])
                source_epoch = int(record.get("source_time_epoch_seconds", payload["time"]))
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                raise ValueError(f"invalid tick record at line {line_number}") from exc
            if not math.isfinite(price) or price <= 0:
                raise ValueError(f"invalid tick price at line {line_number}")
            latest = {
                "symbol": wanted_symbol,
                "price": price,
                "source_time_epoch_seconds": source_epoch,
                "source_timestamp_utc": datetime.fromtimestamp(source_epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
                "received_at_utc": record.get("received_at_utc"),
                "source": record.get("source", "unknown"),
            }
    if latest is None:
        raise FileNotFoundError(f"no tick for {wanted_symbol} found in {path}")
    return latest


class BPR02RequestHandler(BaseHTTPRequestHandler):
    """Read-only JSON HTTP handler. No account or trading routes exist."""

    server_version = "BPR02ReadOnlyAPI/1.0"
    sys_version = ""

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self, route: str) -> bool:
        token = os.environ.get("BPR02_API_TOKEN", "").strip()
        if route == "/health" and not token:
            return True
        if not token:
            return True  # Safe only while bound to loopback; startup enforces that.
        supplied = self.headers.get("Authorization", "")
        expected = f"Bearer {token}"
        return hmac.compare_digest(supplied, expected)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlsplit(self.path)
        route = parsed.path
        if not self._authorized(route):
            self._send_json(401, {"error": "unauthorized", "message": "Provide the configured Bearer token.", "orders_submitted": 0})
            return

        try:
            if route == "/":
                self._send_json(200, {
                    "name": "BPR-02 Read-only Data API",
                    "mode": "local_read_only",
                    "endpoints": ["/health", "/v1/candles", "/v1/quality", "/v1/ticks/latest"],
                    "orders_supported": False,
                })
                return
            if route == "/health":
                self._send_json(200, {
                    "status": "ok",
                    "service": "BPR-02 Read-only Data API",
                    "mode": "local_read_only",
                    "orders_supported": False,
                    "orders_submitted": 0,
                })
                return
            if route == "/v1/candles":
                query = parse_qs(parsed.query, keep_blank_values=True)
                symbol = query.get("symbol", [_symbol()])[0]
                if symbol != _symbol():
                    self._send_json(400, {"error": "symbol_mismatch", "configured_symbol": _symbol(), "requested_symbol": symbol})
                    return
                timeframe = query.get("timeframe_seconds", [str(_timeframe_seconds())])[0]
                try:
                    requested_timeframe = int(timeframe)
                except ValueError:
                    self._send_json(400, {"error": "invalid_timeframe_seconds"})
                    return
                if requested_timeframe != _timeframe_seconds():
                    self._send_json(400, {"error": "timeframe_mismatch", "configured_timeframe_seconds": _timeframe_seconds()})
                    return
                try:
                    limit = int(query.get("limit", ["100"])[0])
                except ValueError:
                    self._send_json(400, {"error": "limit_must_be_an_integer"})
                    return
                if not 1 <= limit <= MAX_CANDLE_LIMIT:
                    self._send_json(400, {"error": "limit_out_of_range", "minimum": 1, "maximum": MAX_CANDLE_LIMIT})
                    return
                try:
                    candles = _load_candles()
                except FileNotFoundError as exc:
                    self._send_json(503, {"error": "data_missing", "message": str(exc), "orders_submitted": 0})
                    return
                self._send_json(200, {
                    "status": "ok",
                    "mode": "read_only_local_dataset",
                    "symbol": _symbol(),
                    "timeframe_seconds": _timeframe_seconds(),
                    "count": min(limit, len(candles)),
                    "candles": candles[-limit:],
                    "source_metadata": _source_metadata(),
                    "orders_submitted": 0,
                })
                return
            if route == "/v1/quality":
                self._send_json(200, _quality_report())
                return
            if route == "/v1/ticks/latest":
                try:
                    tick = _latest_tick()
                except FileNotFoundError as exc:
                    self._send_json(503, {"error": "tick_data_missing", "message": str(exc), "orders_submitted": 0})
                    return
                self._send_json(200, {
                    "status": "ok",
                    "mode": "read_only_local_tick_log",
                    "tick": tick,
                    "orders_submitted": 0,
                })
                return
            self._send_json(404, {"error": "not_found", "available_endpoints": ["/health", "/v1/candles", "/v1/quality", "/v1/ticks/latest"]})
        except (OSError, ValueError, csv.Error) as exc:
            self._send_json(422, {"error": "data_validation_error", "message": str(exc), "orders_submitted": 0})

    def do_POST(self) -> None:  # noqa: N802
        self._send_json(405, {"error": "method_not_allowed", "message": "BPR-02 API is read-only.", "orders_supported": False})

    def do_PUT(self) -> None:  # noqa: N802
        self._send_json(405, {"error": "method_not_allowed", "message": "BPR-02 API is read-only.", "orders_supported": False})

    def do_DELETE(self) -> None:  # noqa: N802
        self._send_json(405, {"error": "method_not_allowed", "message": "BPR-02 API is read-only.", "orders_supported": False})

    def log_message(self, format: str, *args: Any) -> None:
        # Do not log request headers or tokens; keep only standard request diagnostics.
        super().log_message(format, *args)


def _is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    token = os.environ.get("BPR02_API_TOKEN", "").strip()
    if not _is_loopback_host(host) and not token:
        raise SystemExit(
            "Refusing to listen on a non-loopback address without BPR02_API_TOKEN. "
            "Use BPR02_API_HOST=127.0.0.1 for local-only access, or configure a strong token first."
        )
    if not 1 <= port <= 65535:
        raise SystemExit("Port must be between 1 and 65535.")
    server = ThreadingHTTPServer((host, port), BPR02RequestHandler)
    print(f"BPR-02 read-only API listening on http://{host}:{port}", flush=True)
    print("Routes: /health, /v1/candles, /v1/quality, /v1/ticks/latest", flush=True)
    print("No trading/order endpoints are implemented.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping BPR-02 API.", flush=True)
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the local read-only BPR-02 data API.")
    parser.add_argument("--host", default=os.environ.get("BPR02_API_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("BPR02_API_PORT", "8000")))
    args = parser.parse_args(argv)
    serve(args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

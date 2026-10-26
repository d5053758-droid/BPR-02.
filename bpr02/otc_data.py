"""Read-only OTCharts tick recorder and converter for research datasets.

This optional adapter reads a third-party market-data API. It does not access a
Pocket Option login/session and cannot place or modify orders. OTCharts is not
Pocket Option's official API; data provenance, platform parity and the API
plan's permissions must be verified independently.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator, TextIO

from .provenance import sha256_file

BASE_URL = "https://otcharts.com"
DEFAULT_SYMBOL = "EURUSD_otc"
DEFAULT_SOURCE_CLOCK_AHEAD_SECONDS = 7200


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_safe_line(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _iter_sse_events(stream: Iterable[bytes]) -> Iterator[tuple[str, str]]:
    """Yield complete Server-Sent Events from a byte-line iterable."""
    event_name = "message"
    data_lines: list[str] = []
    for raw_line in stream:
        if isinstance(raw_line, bytes):
            line = raw_line.decode("utf-8", errors="strict")
        else:
            line = str(raw_line)
        line = line.rstrip("\r\n")
        if not line:
            if data_lines:
                yield event_name, "\n".join(data_lines)
            event_name = "message"
            data_lines = []
            continue
        if line.startswith(":"):
            # Treat comments as heartbeat events so finite captures can stop during quiet periods.
            yield "heartbeat", "{}"
            continue
        if line.startswith("event:"):
            event_name = line.split(":", 1)[1].strip() or "message"
        elif line.startswith("data:"):
            data_lines.append(line.split(":", 1)[1].lstrip())
        # Ignore id:, retry: and extension fields; they are not needed to replay ticks.
    if data_lines:
        yield event_name, "\n".join(data_lines)


def _validate_symbols(symbols: Iterable[str]) -> list[str]:
    cleaned: list[str] = []
    for symbol in symbols:
        item = symbol.strip()
        if not item:
            continue
        if any(character in item for character in (",", "\n", "\r")):
            raise ValueError(f"invalid symbol value: {symbol!r}")
        if item not in cleaned:
            cleaned.append(item)
    if not cleaned:
        raise ValueError("at least one non-empty symbol is required")
    return cleaned


def record_live_ticks(
    *,
    api_key: str,
    symbols: Iterable[str] = (DEFAULT_SYMBOL,),
    output_path: str | Path,
    duration_seconds: float = 300.0,
    venue: str = "otc",
    source_clock_ahead_seconds: int = DEFAULT_SOURCE_CLOCK_AHEAD_SECONDS,
    append: bool = False,
    urlopen_fn: Callable[..., object] | None = None,
) -> dict[str, object]:
    """Record a provider's read-only SSE stream to append-only JSON Lines.

    The raw provider timestamp is stored unchanged. Its clock adjustment is
    metadata only and is applied later by the converter, so the raw data remain
    auditable if the provider's clock convention changes.
    """
    if not isinstance(api_key, str) or not api_key.strip():
        raise ValueError("OTCHARTS_API_KEY must contain a non-empty API key")
    if venue != "otc":
        raise ValueError("this adapter is intentionally restricted to venue='otc'")
    if isinstance(duration_seconds, bool) or not isinstance(duration_seconds, (int, float)):
        raise ValueError("duration_seconds must be a finite positive number")
    if not math.isfinite(float(duration_seconds)) or float(duration_seconds) <= 0:
        raise ValueError("duration_seconds must be a finite positive number")
    if isinstance(source_clock_ahead_seconds, bool) or not isinstance(source_clock_ahead_seconds, int):
        raise ValueError("source_clock_ahead_seconds must be an integer number of seconds")

    requested_symbols = _validate_symbols(symbols)
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not append:
        raise FileExistsError(
            f"refusing to overwrite existing raw quote log: {target}; use --append to continue it"
        )

    query = urllib.parse.urlencode({
        "venue": venue,
        "symbol": ",".join(requested_symbols),
    })
    url = f"{BASE_URL}/v1/stream?{query}"
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {api_key.strip()}",
            "Accept": "text/event-stream",
            "User-Agent": "BPR-02-read-only-market-data-recorder/1.0",
        },
        method="GET",
    )
    opener = urlopen_fn or urllib.request.urlopen
    mode = "a" if append else "x"
    deadline = time.monotonic() + float(duration_seconds)
    events_seen = 0
    ticks_seen = 0
    unwanted_symbols = 0

    try:
        with opener(request, timeout=60) as response, target.open(
            mode, encoding="utf-8", newline="\n"
        ) as output:
            for event_name, data in _iter_sse_events(response):
                if time.monotonic() >= deadline:
                    break
                try:
                    payload = json.loads(data)
                except json.JSONDecodeError as exc:
                    raise ValueError(f"provider emitted invalid JSON for event {event_name!r}") from exc
                if not isinstance(payload, dict):
                    raise ValueError(f"provider event {event_name!r} must contain a JSON object")

                record: dict[str, object] = {
                    "schema_version": 1,
                    "source": "otcharts",
                    "venue": venue,
                    "event": event_name,
                    "received_at_utc": _utc_now(),
                    "requested_symbols": requested_symbols,
                    "payload": payload,
                    "source_clock_ahead_seconds_configured": source_clock_ahead_seconds,
                }
                if event_name == "tick":
                    symbol = payload.get("symbol")
                    if symbol not in requested_symbols:
                        unwanted_symbols += 1
                        continue
                    try:
                        source_time = int(payload["time"])
                        price = float(payload["price"])
                    except (KeyError, TypeError, ValueError, OverflowError) as exc:
                        raise ValueError("tick event must contain integer 'time' and numeric 'price'") from exc
                    if not math.isfinite(price) or price <= 0:
                        raise ValueError("tick price must be finite and greater than zero")
                    record["source_time_epoch_seconds"] = source_time
                    ticks_seen += 1

                output.write(_json_safe_line(record) + "\n")
                output.flush()
                events_seen += 1
                if time.monotonic() >= deadline:
                    break
    except urllib.error.HTTPError as exc:
        raise ValueError(
            f"OTCharts returned HTTP {exc.code}; confirm the API key, venue access and stream entitlement"
        ) from exc
    except urllib.error.URLError as exc:
        raise ValueError(f"could not reach OTCharts read-only stream: {exc.reason}") from exc

    return {
        "status": "completed",
        "mode": "read_only_otc_tick_capture",
        "source": "OTCharts third-party market-data API",
        "venue": venue,
        "symbols": requested_symbols,
        "raw_log": str(target),
        "events_recorded": events_seen,
        "tick_events_recorded": ticks_seen,
        "ticks_skipped_for_unrequested_symbols": unwanted_symbols,
        "duration_limit_seconds": float(duration_seconds),
        "source_clock_ahead_seconds_configured": source_clock_ahead_seconds,
        "orders_submitted": 0,
        "api_key_written_to_log": False,
        "warning": (
            "Read-only third-party data, not an official Pocket Option API. "
            "No account login or order execution is used. Verify the feed, clock offset, "
            "sampling cadence and terms before using the data for conclusions."
        ),
    }


def convert_tick_log_to_quotes(
    *,
    raw_log_path: str | Path,
    output_path: str | Path,
    symbol: str = DEFAULT_SYMBOL,
    source_clock_ahead_seconds: int | None = None,
    overwrite: bool = False,
) -> dict[str, object]:
    """Convert recorded ticks into unique UTC-second quote observations.

    OTCharts' published Pocket Option clock convention says its OTC timestamps
    are two hours ahead. The adjustment is configurable and recorded in the
    report. The API examples expose tick time in whole seconds, so repeated ticks
    in the same adjusted second are collapsed to the last tick in file order.
    This produces at most one quote per source second; it cannot reconstruct
    subsecond tick ordering or exact broker execution/settlement.
    """
    raw_source = Path(raw_log_path)
    target = Path(output_path)
    if not raw_source.is_file():
        raise FileNotFoundError(f"raw tick log not found: {raw_source}")
    if raw_source.resolve() == target.resolve():
        raise ValueError("CSV output path must differ from the raw JSONL log")
    if source_clock_ahead_seconds is not None and (
        isinstance(source_clock_ahead_seconds, bool) or not isinstance(source_clock_ahead_seconds, int)
    ):
        raise ValueError("source_clock_ahead_seconds must be an integer number of seconds")
    if target.exists() and not overwrite:
        raise FileExistsError(f"refusing to overwrite existing quote CSV: {target}; pass --overwrite")

    quotes_by_epoch: dict[int, float] = {}
    tick_rows = 0
    duplicate_seconds_collapsed = 0
    other_events_ignored = 0
    invalid_rows = 0
    last_epoch: int | None = None
    logged_clock_offset: int | None = None

    with raw_source.open("r", encoding="utf-8") as source:
        for line_number, raw_line in enumerate(source, start=1):
            if not raw_line.strip():
                continue
            try:
                record = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON at raw log line {line_number}") from exc
            if not isinstance(record, dict):
                raise ValueError(f"raw log line {line_number} must be a JSON object")
            recorded_offset = record.get("source_clock_ahead_seconds_configured")
            if recorded_offset is not None:
                if isinstance(recorded_offset, bool) or not isinstance(recorded_offset, int):
                    raise ValueError(f"invalid recorded clock offset at raw log line {line_number}")
                if logged_clock_offset is None:
                    logged_clock_offset = recorded_offset
                elif logged_clock_offset != recorded_offset:
                    raise ValueError(
                        "raw log contains different clock-offset settings; "
                        "keep captures with different settings separate"
                    )
            if record.get("event") != "tick":
                other_events_ignored += 1
                continue
            payload = record.get("payload")
            if not isinstance(payload, dict) or payload.get("symbol") != symbol:
                other_events_ignored += 1
                continue
            try:
                raw_epoch = int(record.get("source_time_epoch_seconds", payload["time"]))
                price = float(payload["price"])
            except (KeyError, TypeError, ValueError, OverflowError) as exc:
                invalid_rows += 1
                raise ValueError(f"invalid tick data at raw log line {line_number}") from exc
            if not math.isfinite(price) or price <= 0:
                invalid_rows += 1
                raise ValueError(f"non-finite or non-positive price at raw log line {line_number}")

            if last_epoch is not None and raw_epoch < last_epoch:
                raise ValueError(
                    f"source tick time moved backward at raw log line {line_number}; "
                    "do not combine recordings with incompatible clock settings"
                )
            last_epoch = raw_epoch
            if raw_epoch in quotes_by_epoch:
                duplicate_seconds_collapsed += 1
            quotes_by_epoch[raw_epoch] = price
            tick_rows += 1

    if not quotes_by_epoch:
        raise ValueError(f"no tick records for symbol {symbol!r} were found in the raw log")
    if source_clock_ahead_seconds is None:
        effective_clock_offset = (
            logged_clock_offset
            if logged_clock_offset is not None
            else DEFAULT_SOURCE_CLOCK_AHEAD_SECONDS
        )
    else:
        effective_clock_offset = source_clock_ahead_seconds
        if logged_clock_offset is not None and logged_clock_offset != effective_clock_offset:
            raise ValueError(
                "requested clock offset does not match the raw log's recorded setting; "
                "use the same value or keep separate captures separate"
            )

    ordered = sorted(
        ((epoch - effective_clock_offset, price) for epoch, price in quotes_by_epoch.items()),
        key=lambda item: item[0],
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["timestamp", "price"])
            for epoch, price in ordered:
                timestamp = datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")
                writer.writerow([timestamp, format(price, ".12g")])
            handle.flush()
            os.fsync(handle.fileno())
        if target.exists() and not overwrite:
            raise FileExistsError(f"refusing to overwrite existing quote CSV: {target}; pass --overwrite")
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()

    gaps = [
        right[0] - left[0]
        for left, right in zip(ordered, ordered[1:])
    ]
    span = ordered[-1][0] - ordered[0][0] if len(ordered) >= 2 else 0
    return {
        "status": "completed",
        "mode": "otc_tick_log_to_second_quotes",
        "source": "OTCharts third-party market-data API",
        "symbol": symbol,
        "raw_log": str(raw_source),
        "raw_log_sha256": sha256_file(raw_source),
        "output_csv": str(target),
        "source_clock_ahead_seconds_subtracted": effective_clock_offset,
        "tick_rows_for_symbol": tick_rows,
        "unique_second_quotes": len(ordered),
        "same_second_ticks_collapsed": duplicate_seconds_collapsed,
        "other_events_ignored": other_events_ignored,
        "invalid_rows": invalid_rows,
        "start_timestamp_utc": datetime.fromtimestamp(ordered[0][0], tz=timezone.utc).isoformat(),
        "end_timestamp_utc": datetime.fromtimestamp(ordered[-1][0], tz=timezone.utc).isoformat(),
        "span_seconds": span,
        "maximum_gap_seconds": max(gaps) if gaps else None,
        "orders_submitted": 0,
        "warning": (
            "Source ticks are exposed with whole-second timestamps in the documented API. "
            "Repeated ticks in a second were collapsed; subsecond sequence and exact broker "
            "settlement cannot be reconstructed from this CSV."
        ),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bpr02-otc-data",
        description="Optional read-only OTCharts OTC recorder and JSONL-to-CSV converter.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    record = subparsers.add_parser("record", help="Capture live read-only OTC tick events to JSONL")
    record.add_argument("--symbols", nargs="+", default=[DEFAULT_SYMBOL])
    record.add_argument("--venue", choices=("otc",), default="otc")
    record.add_argument("--duration-seconds", type=float, default=300.0)
    record.add_argument(
        "--source-clock-ahead-seconds",
        type=int,
        default=DEFAULT_SOURCE_CLOCK_AHEAD_SECONDS,
        help="Clock offset documented by OTCharts for its Pocket Option OTC book; applied during conversion",
    )
    record.add_argument("--output", default="otc_ticks_raw.jsonl")
    record.add_argument("--append", action="store_true")
    record.add_argument("--api-key-env", default="OTCHARTS_API_KEY")

    convert = subparsers.add_parser("convert", help="Convert a raw JSONL tick log to quote CSV")
    convert.add_argument("raw_log")
    convert.add_argument("--symbol", default=DEFAULT_SYMBOL)
    convert.add_argument(
        "--source-clock-ahead-seconds",
        type=int,
        default=None,
        help="Defaults to the offset recorded during capture; older logs without metadata default to 7200 seconds",
    )
    convert.add_argument("--output", default="otc_quotes.csv")
    convert.add_argument("--overwrite", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "record":
            api_key = os.environ.get(args.api_key_env, "")
            result = record_live_ticks(
                api_key=api_key,
                symbols=args.symbols,
                output_path=args.output,
                duration_seconds=args.duration_seconds,
                venue=args.venue,
                source_clock_ahead_seconds=args.source_clock_ahead_seconds,
                append=args.append,
            )
        else:
            result = convert_tick_log_to_quotes(
                raw_log_path=args.raw_log,
                output_path=args.output,
                symbol=args.symbol,
                source_clock_ahead_seconds=args.source_clock_ahead_seconds,
                overwrite=args.overwrite,
            )
        print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
        return 0
    except (FileExistsError, FileNotFoundError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

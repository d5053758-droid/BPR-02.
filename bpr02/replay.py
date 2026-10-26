"""Replay a historical OHLC CSV as closed-candle JSON Lines.

This is a deterministic test adapter, not a real-time feed or Pocket Option
connection. Use it to verify the complete pipeline before integrating any
provider-authorized source.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time

from .data import load_ohlc_csv


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bpr02-replay",
        description="Replay historical CSV candles as JSON Lines; no network or orders.",
    )
    parser.add_argument("csv", help="Historical OHLC CSV path")
    parser.add_argument("--symbol", default="EURUSD", help="Symbol label in emitted records")
    parser.add_argument("--timeframe", default="M1", help="Timeframe label in emitted records")
    parser.add_argument("--source", default="historical_csv_replay", help="Descriptive source label")
    parser.add_argument(
        "--interval-seconds", type=float, default=0.0,
        help="Seconds to wait between candles; 0 emits as fast as possible",
    )
    parser.add_argument("--start-index", type=int, default=0, help="Zero-based first row to emit")
    parser.add_argument("--limit", type=int, help="Maximum number of candles to emit")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if not math.isfinite(args.interval_seconds) or args.interval_seconds < 0:
            raise ValueError("--interval-seconds must be finite and >= 0")
        if args.start_index < 0:
            raise ValueError("--start-index must be >= 0")
        if args.limit is not None and args.limit < 1:
            raise ValueError("--limit must be >= 1")

        candles = load_ohlc_csv(args.csv)
        if args.start_index >= len(candles):
            raise ValueError("--start-index is at or beyond the end of the CSV")
        stop = len(candles) if args.limit is None else min(
            len(candles), args.start_index + args.limit
        )
        selected = candles.iloc[args.start_index:stop]
        for row in selected.to_dict(orient="records"):
            timestamp = row["timestamp"]
            event = {
                "timestamp": timestamp.isoformat(),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "closed": True,
                "symbol": args.symbol,
                "timeframe": args.timeframe,
                "source": args.source,
                "source_mode": "historical_replay_not_live",
            }
            print(json.dumps(event, separators=(",", ":"), ensure_ascii=False), flush=True)
            if args.interval_seconds:
                time.sleep(args.interval_seconds)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

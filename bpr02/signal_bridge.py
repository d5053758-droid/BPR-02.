"""JSON Lines bridge from an authorized closed-candle feed to research alerts.

This module has deliberately no networking, broker login, browser automation,
or order execution. Feed adapters should pass only completed candles.
"""
from __future__ import annotations

import hashlib
import json
import math
import sys
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import TextIO

import pandas as pd

from .data import load_ohlc_csv
from .signals import NO_TRADE, sweep_reclaim_signals


def _iso_utc(value: object) -> str:
    timestamp = pd.to_datetime(value, errors="raise", utc=True)
    if pd.isna(timestamp):
        raise ValueError("timestamp is missing or invalid")
    return timestamp.isoformat()


def _validate_candle(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise ValueError("each JSON Lines input must be a JSON object")
    if payload.get("closed") is not True:
        raise ValueError("only completed candles are accepted; set 'closed': true")
    if "timestamp" not in payload:
        raise ValueError("candle requires a timestamp")
    candle: dict[str, object] = {"timestamp": _iso_utc(payload["timestamp"])}
    for column in ("open", "high", "low", "close"):
        try:
            value = float(payload[column])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"candle requires numeric {column!r}") from exc
        if not math.isfinite(value):
            raise ValueError(f"candle {column!r} must be finite")
        candle[column] = value
    if candle["high"] < max(candle["open"], candle["close"], candle["low"]):
        raise ValueError("candle high is inconsistent with OHLC prices")
    if candle["low"] > min(candle["open"], candle["close"], candle["high"]):
        raise ValueError("candle low is inconsistent with OHLC prices")
    for optional in ("symbol", "timeframe", "source"):
        value = payload.get(optional)
        if value is not None:
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{optional} must be a non-empty string when supplied")
            candle[optional] = value.strip()
    return candle


def _event_id(event: dict[str, object]) -> str:
    identity = {
        "timestamp": event["timestamp"],
        "symbol": event["symbol"],
        "timeframe": event["timeframe"],
        "source": event["source"],
        "lookback": event["lookback"],
        "signal": event["signal"],
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:20]


class SignalBridge:
    """Produce one deterministic research event per new completed candle."""

    def __init__(
        self,
        *,
        lookback: int = 12,
        expiry_bars: int = 1,
        symbol: str = "UNKNOWN",
        timeframe: str = "UNKNOWN",
        source: str = "authorized_feed",
        history: pd.DataFrame | None = None,
    ) -> None:
        if lookback < 2:
            raise ValueError("lookback must be >= 2")
        if expiry_bars < 1:
            raise ValueError("expiry_bars must be >= 1")
        self.lookback = lookback
        self.expiry_bars = expiry_bars
        self.symbol = symbol
        self.timeframe = timeframe
        self.source = source
        self._candles: deque[dict[str, object]] = deque(maxlen=lookback + 1)
        self._last_timestamp: pd.Timestamp | None = None
        if history is not None:
            required = {"timestamp", "open", "high", "low", "close"}
            if not required.issubset(history.columns):
                raise ValueError(f"history must contain {sorted(required)}")
            ordered = history.copy()
            ordered["timestamp"] = pd.to_datetime(ordered["timestamp"], errors="raise", utc=True)
            if ordered["timestamp"].isna().any() or ordered["timestamp"].duplicated().any():
                raise ValueError("history timestamps must be valid and unique")
            if not ordered["timestamp"].is_monotonic_increasing:
                raise ValueError("history must be chronologically sorted")
            for record in ordered.tail(lookback + 1).to_dict(orient="records"):
                payload = dict(record)
                payload["closed"] = True
                validated = _validate_candle(payload)
                self._candles.append(validated)
                self._last_timestamp = pd.Timestamp(validated["timestamp"])

    def process(self, payload: object) -> dict[str, object] | None:
        """Return an event for a new closed candle; reject duplicate/out-of-order bars."""
        candle = _validate_candle(payload)
        timestamp = pd.Timestamp(candle["timestamp"])
        if self._last_timestamp is not None:
            if timestamp == self._last_timestamp:
                raise ValueError("duplicate candle timestamp; repeated/updated candle rejected")
            if timestamp < self._last_timestamp:
                raise ValueError("candle timestamp moved backwards")
        self._last_timestamp = timestamp
        self._candles.append(candle)

        if len(self._candles) < self.lookback + 1:
            signal = NO_TRADE
            reason = f"Warm-up: need {self.lookback + 1} closed candles"
        else:
            frame = pd.DataFrame(list(self._candles))
            generated = sweep_reclaim_signals(frame, lookback=self.lookback)
            signal = str(generated.iloc[-1]["signal"])
            reason = str(generated.iloc[-1]["reason"])

        event: dict[str, object] = {
            "event_id": "",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "mode": "research_only",
            "model": "sweep_reclaim_v1",
            "timestamp": candle["timestamp"],
            "symbol": str(candle.get("symbol", self.symbol)),
            "timeframe": str(candle.get("timeframe", self.timeframe)),
            "source": str(candle.get("source", self.source)),
            "lookback": self.lookback,
            "expiry_bars": self.expiry_bars,
            "signal": signal,
            "reason": reason,
            "entry_rule": "next_completed_candle_open_only_if_manually_validated",
            "order_sent": False,
            "manual_review_required": True,
        }
        event["event_id"] = _event_id(event)
        return event


def run_jsonl_bridge(
    input_stream: TextIO,
    output_stream: TextIO,
    *,
    events_path: str | Path,
    bridge: SignalBridge,
) -> int:
    """Read completed candles from JSON Lines and emit/append research events."""
    destination = Path(events_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    processed = 0
    with destination.open("a", encoding="utf-8") as event_file:
        for line_number, line in enumerate(input_stream, start=1):
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
                event = bridge.process(payload)
                if event is None:
                    continue
            except (json.JSONDecodeError, ValueError, TypeError) as exc:
                error = {
                    "mode": "research_only",
                    "event_type": "input_error",
                    "line": line_number,
                    "error": str(exc),
                    "order_sent": False,
                }
                output_stream.write(json.dumps(error, ensure_ascii=False) + "\n")
                output_stream.flush()
                continue
            serialized = json.dumps(event, ensure_ascii=False, sort_keys=True, default=str)
            event_file.write(serialized + "\n")
            event_file.flush()
            output_stream.write(serialized + "\n")
            output_stream.flush()
            processed += 1
    return processed


def build_parser():
    import argparse

    parser = argparse.ArgumentParser(
        prog="bpr02-stream",
        description="Read completed candles from stdin JSONL and emit research alerts; never places orders.",
    )
    parser.add_argument("--history-csv", help="Optional historical CSV to warm up indicator history")
    parser.add_argument("--lookback", type=int, default=12)
    parser.add_argument("--expiry-bars", type=int, default=1)
    parser.add_argument("--symbol", default="UNKNOWN", help="Default symbol label attached to events")
    parser.add_argument("--timeframe", default="UNKNOWN", help="Default timeframe label attached to events")
    parser.add_argument("--source", default="authorized_feed", help="Descriptive feed label; no feed connection is opened")
    parser.add_argument("--events-out", default="bpr02_events.jsonl", help="Append-only JSON Lines event log")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        history = load_ohlc_csv(args.history_csv) if args.history_csv else None
        bridge = SignalBridge(
            lookback=args.lookback,
            expiry_bars=args.expiry_bars,
            symbol=args.symbol,
            timeframe=args.timeframe,
            source=args.source,
            history=history,
        )
        run_jsonl_bridge(sys.stdin, sys.stdout, events_path=args.events_out, bridge=bridge)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Research-only live signal monitor fed by the third-party OTCharts SSE stream.

This module builds complete M1 candles from received OTC ticks and sends those
closed candles into SignalBridge. It never connects to a Pocket Option account,
uses broker session credentials, or submits orders. OTCharts is a third-party
data provider, not Pocket Option's official trading API.
"""
from __future__ import annotations

import argparse
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
from typing import Callable, TextIO

from .otc_data import (
    BASE_URL,
    DEFAULT_SOURCE_CLOCK_AHEAD_SECONDS,
    _iter_sse_events,
    _json_safe_line,
    _validate_symbols,
)
from .signal_bridge import SignalBridge
from .signals import NO_TRADE
from .whatsapp_alerts import WhatsAppDeliveryError, WhatsAppTemplateNotifier

TIMEFRAME_SECONDS = 60
TIMEFRAME_LABEL = "M1"
SOURCE_LABEL = "otcharts_live_stream"


def _utc_iso(epoch_seconds: int) -> str:
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).isoformat().replace("+00:00", "Z")


class _MinuteCandle:
    """Small in-memory OHLC accumulator for one UTC minute."""

    def __init__(self, bucket_start: int, price: float) -> None:
        self.bucket_start = bucket_start
        self.open = price
        self.high = price
        self.low = price
        self.close = price
        self.ticks = 1

    def add(self, price: float) -> None:
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price
        self.ticks += 1

    def closed_payload(self, *, symbol: str) -> dict[str, object]:
        close_epoch = self.bucket_start + TIMEFRAME_SECONDS
        return {
            "timestamp": _utc_iso(close_epoch),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "closed": True,
            "symbol": symbol,
            "timeframe": TIMEFRAME_LABEL,
            "source": SOURCE_LABEL,
            "source_mode": "live_stream_research_only",
        }

    def public_record(self) -> dict[str, object]:
        return {
            "timestamp_open_utc": _utc_iso(self.bucket_start),
            "timestamp_close_utc": _utc_iso(self.bucket_start + TIMEFRAME_SECONDS),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "ticks_used": self.ticks,
        }


def monitor_live_signals(
    *,
    api_key: str,
    output_path: str | Path = "bpr02_live_signals.jsonl",
    symbol: str = "EURUSD_otc",
    lookback: int = 12,
    duration_seconds: float = 300.0,
    source_clock_ahead_seconds: int = DEFAULT_SOURCE_CLOCK_AHEAD_SECONDS,
    max_tick_age_seconds: float = 10.0,
    append: bool = False,
    urlopen_fn: Callable[..., object] | None = None,
    monotonic_fn: Callable[[], float] | None = None,
    now_epoch_fn: Callable[[], float] | None = None,
    stdout: TextIO | None = None,
    whatsapp_notifier: WhatsAppTemplateNotifier | None = None,
) -> dict[str, object]:
    """Read one authorized OTC SSE stream and emit research-only M1 signals.

    The latest, still-forming minute is discarded at shutdown; only a later
    minute's first tick confirms that a candle is closed. Any stale tick or
    missing minute resets candle lookback, so the detector cannot bridge a gap.
    The API key is sent only in the HTTP Authorization header and is never
    written to logs or stdout.
    """
    if not isinstance(api_key, str) or not api_key.strip():
        raise ValueError("OTCHARTS_API_KEY must contain a non-empty API key")
    if not isinstance(lookback, int) or isinstance(lookback, bool) or lookback < 2:
        raise ValueError("lookback must be an integer >= 2")
    if isinstance(duration_seconds, bool) or not isinstance(duration_seconds, (int, float)):
        raise ValueError("duration_seconds must be finite and greater than zero")
    if not math.isfinite(float(duration_seconds)) or float(duration_seconds) <= 0:
        raise ValueError("duration_seconds must be finite and greater than zero")
    if isinstance(source_clock_ahead_seconds, bool) or not isinstance(source_clock_ahead_seconds, int):
        raise ValueError("source_clock_ahead_seconds must be an integer")
    if (
        isinstance(max_tick_age_seconds, bool)
        or not isinstance(max_tick_age_seconds, (int, float))
        or not math.isfinite(float(max_tick_age_seconds))
        or float(max_tick_age_seconds) < 0
    ):
        raise ValueError("max_tick_age_seconds must be finite and >= 0")

    requested = _validate_symbols([symbol])
    if len(requested) != 1:
        raise ValueError("this first monitor supports exactly one symbol")
    symbol = requested[0]
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not append:
        raise FileExistsError(
            f"refusing to overwrite live-signal log: {target}; pass --append to continue it"
        )

    query = urllib.parse.urlencode({"venue": "otc", "symbol": symbol})
    request = urllib.request.Request(
        f"{BASE_URL}/v1/stream?{query}",
        headers={
            "Authorization": f"Bearer {api_key.strip()}",
            "Accept": "text/event-stream",
            "User-Agent": "BPR-02-live-research-monitor/1.0",
        },
        method="GET",
    )
    opener = urlopen_fn or urllib.request.urlopen
    monotonic = monotonic_fn or time.monotonic
    now_epoch = now_epoch_fn or time.time
    output_stream = stdout or sys.stdout

    counts: dict[str, int] = {
        "events_written": 0,
        "ticks_seen": 0,
        "ticks_ignored_other_symbol": 0,
        "ticks_discarded_stale": 0,
        "heartbeats_seen": 0,
        "closed_candles": 0,
        "signals_call": 0,
        "signals_put": 0,
        "no_trade_events": 0,
        "data_gaps": 0,
    }
    connected = False
    connection_started: float | None = None
    stop_reason = "stream_ended_before_duration"
    current: _MinuteCandle | None = None
    last_source_epoch: int | None = None
    bridge = SignalBridge(
        lookback=lookback,
        expiry_bars=1,
        symbol=symbol,
        timeframe=TIMEFRAME_LABEL,
        source=SOURCE_LABEL,
    )

    def emit(log_file: TextIO, event: dict[str, object]) -> None:
        event.setdefault("mode", "research_only")
        event.setdefault("orders_submitted", 0)
        event.setdefault("api_key_written_to_output", False)
        serialized = _json_safe_line(event)
        log_file.write(serialized + "\n")
        log_file.flush()
        output_stream.write(serialized + "\n")
        output_stream.flush()
        counts["events_written"] += 1

        # Notifications are opt-in and limited to directional research candidates.
        # No-trade, status, or data-quality events never trigger WhatsApp.
        if (
            whatsapp_notifier is not None
            and event.get("event_type") == "research_signal"
            and event.get("signal") in {"CALL", "PUT"}
        ):
            try:
                result = whatsapp_notifier.send_event(event)
                notification_event: dict[str, object] = {
                    "event_type": "whatsapp_notification",
                    "source_event_id": event.get("event_id"),
                    "signal": event.get("signal"),
                    **result,
                }
            except Exception as exc:
                notification_event = {
                    "event_type": "whatsapp_notification",
                    "source_event_id": event.get("event_id"),
                    "signal": event.get("signal"),
                    "status": "failed",
                    "error_type": type(exc).__name__,
                    "reason": (
                        str(exc) if isinstance(exc, WhatsAppDeliveryError)
                        else "unexpected notification error; details withheld"
                    ),
                    "orders_submitted": 0,
                }
            emit(log_file, notification_event)

    try:
        response_context = opener(request, timeout=60)
        with response_context as response:
            mode = "a" if append else "x"
            with target.open(mode, encoding="utf-8", newline="\n") as log_file:
                for event_name, data in _iter_sse_events(response):
                    if connected and connection_started is not None:
                        if monotonic() - connection_started >= float(duration_seconds):
                            stop_reason = "duration_elapsed"
                            break

                    if event_name == "heartbeat":
                        counts["heartbeats_seen"] += 1
                        continue

                    try:
                        payload = json.loads(data)
                    except json.JSONDecodeError as exc:
                        raise ValueError(
                            f"OTCharts emitted invalid JSON for SSE event {event_name!r}"
                        ) from exc
                    if not isinstance(payload, dict):
                        raise ValueError(
                            f"OTCharts SSE event {event_name!r} must contain a JSON object"
                        )

                    if event_name == "connected":
                        symbols = payload.get("symbols")
                        if not isinstance(symbols, list) or not all(
                            isinstance(item, str) for item in symbols
                        ):
                            raise ValueError("OTCharts connected event has no valid symbols list")
                        if symbol not in symbols:
                            emit(log_file, {
                                "event_type": "subscription_rejected",
                                "symbol": symbol,
                                "subscribed_symbols": symbols,
                                "reason": "requested symbol was not present in the provider's connected list",
                            })
                            stop_reason = "symbol_not_subscribed"
                            break
                        connected = True
                        connection_started = monotonic()
                        emit(log_file, {
                            "event_type": "stream_connected",
                            "provider": "OTCharts third-party market-data API",
                            "venue": "otc",
                            "symbol": symbol,
                            "timeframe": TIMEFRAME_LABEL,
                            "duration_limit_seconds": float(duration_seconds),
                            "max_tick_age_seconds": float(max_tick_age_seconds),
                            "source_clock_ahead_seconds_subtracted": source_clock_ahead_seconds,
                            "execution_enabled": False,
                        })
                        continue

                    if event_name in {"closed", "dropped", "replaced"}:
                        affected = payload.get(event_name)
                        if event_name == "replaced" or not isinstance(affected, list) or symbol in affected:
                            stop_reason = {
                                "closed": "instrument_closed",
                                "dropped": "symbol_dropped",
                                "replaced": "stream_replaced",
                            }[event_name]
                            emit(log_file, {
                                "event_type": "stream_stopped",
                                "status": stop_reason,
                                "symbol": symbol,
                                "provider_event": event_name,
                                "detail": payload.get("reason", "provider stopped or refused this subscription"),
                            })
                            break
                        continue

                    if event_name != "tick":
                        # Unknown provider event types are not interpreted as market data.
                        emit(log_file, {
                            "event_type": "provider_event_ignored",
                            "provider_event": event_name,
                        })
                        continue

                    if not connected:
                        raise ValueError("received a tick before OTCharts confirmed the subscription")
                    tick_symbol = payload.get("symbol")
                    if tick_symbol != symbol:
                        counts["ticks_ignored_other_symbol"] += 1
                        continue
                    raw_time = payload.get("time")
                    if isinstance(raw_time, bool):
                        raise ValueError("tick time must be an integer epoch timestamp")
                    try:
                        source_epoch = int(raw_time)
                        price = float(payload["price"])
                    except (TypeError, ValueError, KeyError, OverflowError) as exc:
                        raise ValueError("tick requires integer 'time' and numeric 'price'") from exc
                    if raw_time is None or str(raw_time).strip() != str(source_epoch):
                        raise ValueError("tick time must be an integer epoch timestamp")
                    if source_epoch <= 0:
                        raise ValueError("tick time must be positive")
                    if not math.isfinite(price) or price <= 0:
                        raise ValueError("tick price must be finite and greater than zero")
                    if last_source_epoch is not None and source_epoch < last_source_epoch:
                        raise ValueError("OTCharts tick timestamp moved backwards")
                    last_source_epoch = source_epoch
                    corrected_epoch = source_epoch - source_clock_ahead_seconds
                    age_seconds = float(now_epoch()) - corrected_epoch
                    counts["ticks_seen"] += 1
                    if age_seconds < -2.0:
                        emit(log_file, {
                            "event_type": "data_quality_warning",
                            "symbol": symbol,
                            "reason": "corrected provider timestamp is unexpectedly in the future",
                            "tick_age_seconds": round(age_seconds, 3),
                        })
                        current = None
                        bridge = SignalBridge(
                            lookback=lookback,
                            expiry_bars=1,
                            symbol=symbol,
                            timeframe=TIMEFRAME_LABEL,
                            source=SOURCE_LABEL,
                        )
                        continue
                    if age_seconds > float(max_tick_age_seconds):
                        counts["ticks_discarded_stale"] += 1
                        emit(log_file, {
                            "event_type": "stale_tick_discarded",
                            "symbol": symbol,
                            "tick_age_seconds": round(age_seconds, 3),
                            "max_tick_age_seconds": float(max_tick_age_seconds),
                            "reason": "stale data are not allowed to generate a signal",
                        })
                        current = None
                        bridge = SignalBridge(
                            lookback=lookback,
                            expiry_bars=1,
                            symbol=symbol,
                            timeframe=TIMEFRAME_LABEL,
                            source=SOURCE_LABEL,
                        )
                        continue

                    bucket_start = corrected_epoch - (corrected_epoch % TIMEFRAME_SECONDS)
                    if current is None:
                        current = _MinuteCandle(bucket_start, price)
                        continue

                    if bucket_start < current.bucket_start:
                        raise ValueError("OTCharts tick timestamp moved to an earlier candle bucket")
                    if bucket_start == current.bucket_start:
                        current.add(price)
                        continue

                    interval_delta = bucket_start - current.bucket_start
                    if interval_delta == TIMEFRAME_SECONDS:
                        closed_payload = current.closed_payload(symbol=symbol)
                        signal_event = bridge.process(closed_payload)
                        if signal_event is not None:
                            signal_name = str(signal_event.get("signal", NO_TRADE))
                            counts["closed_candles"] += 1
                            if signal_name == "CALL":
                                counts["signals_call"] += 1
                            elif signal_name == "PUT":
                                counts["signals_put"] += 1
                            else:
                                counts["no_trade_events"] += 1
                            signal_event.update({
                                "event_type": "research_signal" if signal_name in {"CALL", "PUT"} else "no_trade",
                                "source_mode": "live_stream_research_only",
                                "candle": current.public_record(),
                                "entry_instruction": "none; research alert only",
                                "orders_submitted": 0,
                                "api_key_written_to_output": False,
                            })
                            emit(log_file, signal_event)
                    else:
                        missing_intervals = max(0, interval_delta // TIMEFRAME_SECONDS - 1)
                        counts["data_gaps"] += 1
                        emit(log_file, {
                            "event_type": "data_gap",
                            "symbol": symbol,
                            "timeframe": TIMEFRAME_LABEL,
                            "last_candle_open_utc": _utc_iso(current.bucket_start),
                            "next_observed_candle_open_utc": _utc_iso(bucket_start),
                            "estimated_missing_minutes": missing_intervals,
                            "reason": "previous forming candle discarded and signal lookback reset; no missing prices are fabricated",
                        })
                        bridge = SignalBridge(
                            lookback=lookback,
                            expiry_bars=1,
                            symbol=symbol,
                            timeframe=TIMEFRAME_LABEL,
                            source=SOURCE_LABEL,
                        )

                    current = _MinuteCandle(bucket_start, price)

                else:
                    # The SSE iterable ended without an explicit stop event.
                    if connected and connection_started is not None:
                        elapsed = monotonic() - connection_started
                        stop_reason = (
                            "duration_elapsed" if elapsed >= float(duration_seconds)
                            else "stream_ended_before_duration"
                        )
                    else:
                        stop_reason = "subscription_never_confirmed"
    except urllib.error.HTTPError as exc:
        raise ValueError(
            f"OTCharts returned HTTP {exc.code}; confirm the API key, OTC access and live-stream entitlement"
        ) from exc
    except urllib.error.URLError as exc:
        raise ValueError(f"could not reach OTCharts live stream: {exc.reason}") from exc

    partial_candle_discarded = current is not None
    summary: dict[str, object] = {
        "status": stop_reason,
        "mode": "research_only_live_tick_monitor",
        "provider": "OTCharts third-party market-data API",
        "symbol": symbol,
        "timeframe": TIMEFRAME_LABEL,
        "duration_limit_seconds": float(duration_seconds),
        "counts": counts,
        "connected_confirmed": connected,
        "partial_candle_discarded_at_stop": partial_candle_discarded,
        "orders_submitted": 0,
        "api_key_written_to_output": False,
        "whatsapp_notifications_enabled": whatsapp_notifier is not None,
        "warning": (
            "This monitor generates research alerts from closed M1 candles. "
            "It does not connect to a Pocket Option account, establish exact price parity, "
            "reconstruct three-second settlement, or send any order."
        ),
    }
    return summary


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bpr02-live-signals",
        description=(
            "Read-only OTCharts SSE monitor that creates M1 research alerts; "
            "no Pocket Option login and no order execution."
        ),
    )
    parser.add_argument("--symbol", default="EURUSD_otc", help="OTCharts OTC instrument id")
    parser.add_argument("--lookback", type=int, default=12)
    parser.add_argument("--duration-seconds", type=float, default=300.0)
    parser.add_argument(
        "--source-clock-ahead-seconds",
        type=int,
        default=DEFAULT_SOURCE_CLOCK_AHEAD_SECONDS,
        help="Subtract the provider-documented UTC+2 OTC clock offset (default 7200)",
    )
    parser.add_argument(
        "--max-tick-age-seconds",
        type=float,
        default=10.0,
        help="Discard stale ticks and reset signal lookback when age exceeds this limit",
    )
    parser.add_argument("--output", default="bpr02_live_signals.jsonl")
    parser.add_argument("--append", action="store_true", help="Append to an existing research-event log")
    parser.add_argument("--api-key-env", default="OTCHARTS_API_KEY")
    parser.add_argument(
        "--whatsapp-send",
        action="store_true",
        help="explicitly enable real WhatsApp template alerts for CALL/PUT research candidates",
    )
    parser.add_argument(
        "--whatsapp-sent-ids",
        default=os.environ.get("WHATSAPP_SENT_IDS_PATH", "bpr02_whatsapp_sent.jsonl"),
        help="append-only local de-duplication record; never contains API tokens or alert text",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        api_key = os.environ.get(args.api_key_env, "")
        whatsapp_notifier = (
            WhatsAppTemplateNotifier.from_env(sent_ids_path=args.whatsapp_sent_ids)
            if args.whatsapp_send else None
        )
        result = monitor_live_signals(
            api_key=api_key,
            output_path=args.output,
            symbol=args.symbol,
            lookback=args.lookback,
            duration_seconds=args.duration_seconds,
            source_clock_ahead_seconds=args.source_clock_ahead_seconds,
            max_tick_age_seconds=args.max_tick_age_seconds,
            append=args.append,
            whatsapp_notifier=whatsapp_notifier,
        )
    except (FileExistsError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print("MONITOR_SUMMARY " + json.dumps(result, ensure_ascii=False, allow_nan=False), file=sys.stderr)
    return 0 if result["status"] == "duration_elapsed" else 3


if __name__ == "__main__":
    raise SystemExit(main())

"""Sub-minute fixed-expiry research using timestamped quote observations.

This module is separate from the OHLC bar-expiry backtester. It can estimate
second-based settlement only when supplied with a suitable timestamped price
series from the same market/feed whose outcomes are being studied. Synthetic
or conventional EUR/USD quotes do not validate Pocket Option EUR/USD OTC.
Nothing in this module sends orders or contacts a broker.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path
from typing import Literal

import pandas as pd

from .data import load_ohlc_csv
from .provenance import build_run_manifest, write_run_manifest
from .signals import CALL, NO_TRADE, PUT, sweep_reclaim_signals
from .backtest import summarize_results

TiePolicy = Literal["push", "loss"]
BarTimestampLabel = Literal["open", "close"]


def _as_finite_number(value: object, name: str, *, minimum: float, inclusive: bool) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be a finite number")
    valid = result >= minimum if inclusive else result > minimum
    if not valid:
        operator = ">=" if inclusive else ">"
        raise ValueError(f"{name} must be {operator} {minimum}")
    return result


def _parse_explicit_utc(values: pd.Series, *, field_name: str) -> pd.Series:
    """Parse timestamps without silently treating timezone-naive input as UTC."""
    parsed: list[pd.Timestamp] = []
    for row_number, value in enumerate(values.tolist(), start=1):
        try:
            timestamp = pd.Timestamp(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{field_name} has an invalid timestamp at row {row_number}") from exc
        if pd.isna(timestamp):
            raise ValueError(f"{field_name} has a missing timestamp at row {row_number}")
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError(
                f"{field_name} timestamps must include a timezone/UTC offset; "
                f"row {row_number} is timezone-naive"
            )
        parsed.append(timestamp.tz_convert("UTC"))
    return pd.Series(pd.DatetimeIndex(parsed), index=values.index, name="timestamp")


def load_quote_csv(path: str | Path) -> pd.DataFrame:
    """Load timestamped scalar quotes from CSV with explicit timezone information.

    Required columns: timestamp, price. Timestamps must be unique and sorted
    ascending. Price must be finite and greater than zero. The price column
    must represent the same quote convention used by the target platform.
    """
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(f"quote CSV not found: {source}")
    try:
        frame = pd.read_csv(source)
    except Exception as exc:
        raise ValueError(f"could not read quote CSV: {exc}") from exc
    frame = frame.rename(columns={str(column): str(column).strip().lower() for column in frame.columns})
    required = {"timestamp", "price"}
    missing = sorted(required.difference(frame.columns))
    if missing:
        raise ValueError(f"quote CSV must contain columns {sorted(required)}; missing {missing}")
    if frame.empty:
        raise ValueError("quote CSV contains no rows")

    frame["timestamp"] = _parse_explicit_utc(frame["timestamp"], field_name="quote CSV")
    if frame["timestamp"].duplicated().any():
        raise ValueError("quote timestamps must be unique")
    if not frame["timestamp"].is_monotonic_increasing:
        raise ValueError("quote timestamps must be chronologically sorted")

    try:
        prices = pd.to_numeric(frame["price"], errors="raise").astype(float)
    except (TypeError, ValueError) as exc:
        raise ValueError("quote prices must be numeric and finite") from exc
    if prices.isna().any() or not prices.map(math.isfinite).all():
        raise ValueError("quote prices must be numeric and finite")
    if (prices <= 0).any():
        raise ValueError("quote prices must be greater than zero")
    return pd.DataFrame({"timestamp": frame["timestamp"], "price": prices})


def _validate_candles_and_signals(
    candles: pd.DataFrame,
    signals: pd.Series | pd.DataFrame,
) -> tuple[pd.DataFrame, pd.Series]:
    required = {"timestamp", "open", "high", "low", "close"}
    missing = sorted(required.difference(candles.columns))
    if missing:
        raise ValueError(f"signal candles must contain {sorted(required)}; missing {missing}")
    if candles.empty:
        raise ValueError("signal candles must contain at least one row")
    frame = candles.copy().reset_index(drop=True)
    frame["timestamp"] = _parse_explicit_utc(frame["timestamp"], field_name="signal candles")
    if frame["timestamp"].duplicated().any():
        raise ValueError("signal candle timestamps must be unique")
    if not frame["timestamp"].is_monotonic_increasing:
        raise ValueError("signal candles must be chronologically sorted")

    for column in ("open", "high", "low", "close"):
        try:
            prices = pd.to_numeric(frame[column], errors="raise").astype(float)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"signal candle {column} values must be numeric and finite") from exc
        if prices.isna().any() or not prices.map(math.isfinite).all():
            raise ValueError(f"signal candle {column} values must be numeric and finite")
        frame[column] = prices

    bad_high = frame["high"] < frame[["open", "close", "low"]].max(axis=1)
    bad_low = frame["low"] > frame[["open", "close", "high"]].min(axis=1)
    if (bad_high | bad_low).any():
        rows = [position + 1 for position, invalid in enumerate((bad_high | bad_low).to_numpy()) if invalid][:5]
        raise ValueError(f"invalid OHLC range in signal candles at row(s): {rows}")

    if isinstance(signals, pd.DataFrame):
        if "signal" not in signals.columns:
            raise ValueError("signals DataFrame must contain a 'signal' column")
        series = signals["signal"]
    else:
        series = signals
    if len(series) != len(frame):
        raise ValueError("signals and signal candles must have the same number of rows")
    series = series.astype(str).str.strip().str.upper().reset_index(drop=True)
    invalid = sorted(set(series.unique()).difference({CALL, PUT, NO_TRADE}))
    if invalid:
        raise ValueError(f"signals contain unsupported label(s): {invalid}")
    return frame, series


def backtest_seconds_expiry(
    signal_candles: pd.DataFrame,
    signals: pd.Series | pd.DataFrame,
    quotes: pd.DataFrame,
    *,
    bar_seconds: float = 60.0,
    bar_timestamp_label: BarTimestampLabel = "open",
    expiry_seconds: float = 3.0,
    entry_delay_seconds: float = 0.0,
    max_quote_delay_seconds: float = 1.0,
    payout: float = 0.80,
    stake: float = 1.0,
    tie_policy: TiePolicy = "push",
) -> tuple[pd.DataFrame, dict[str, int | float | str]]:
    """Model an expiry in seconds from timestamped quote observations.

    signal_candles timestamps identify either bar-open or bar-close time as
    specified by bar_timestamp_label. With "open", a signal becomes available
    at bar timestamp + bar_seconds; with "close", it becomes available at the
    timestamp itself.

    Entry price is the first quote at or after signal availability plus the
    configured entry delay. Settlement price is the first quote at or after
    actual entry timestamp + expiry_seconds. If no quote exists, or the first
    eligible quote exceeds max_quote_delay_seconds, that signal is counted as
    unmodeled and no trade outcome is fabricated.

    The quote series must represent the target platform's relevant price feed
    and settlement convention. This does not reproduce execution latency,
    rejected orders, OTC-specific quotes, variable payout or broker rules.
    """
    bars_seconds = _as_finite_number(bar_seconds, "bar_seconds", minimum=0, inclusive=False)
    expiry = _as_finite_number(expiry_seconds, "expiry_seconds", minimum=0, inclusive=False)
    entry_delay = _as_finite_number(entry_delay_seconds, "entry_delay_seconds", minimum=0, inclusive=True)
    max_delay = _as_finite_number(max_quote_delay_seconds, "max_quote_delay_seconds", minimum=0, inclusive=True)
    payout_value = _as_finite_number(payout, "payout", minimum=0, inclusive=True)
    if payout_value > 1:
        raise ValueError("payout must be between 0 and 1")
    stake_value = _as_finite_number(stake, "stake", minimum=0, inclusive=False)
    if tie_policy not in ("push", "loss"):
        raise ValueError("tie_policy must be 'push' or 'loss'")
    if bar_timestamp_label not in ("open", "close"):
        raise ValueError("bar_timestamp_label must be 'open' or 'close'")

    candles, signal_series = _validate_candles_and_signals(signal_candles, signals)
    required_quotes = {"timestamp", "price"}
    missing = sorted(required_quotes.difference(quotes.columns))
    if missing:
        raise ValueError(f"quotes must contain {sorted(required_quotes)}; missing {missing}")
    if quotes.empty:
        raise ValueError("quotes must contain at least one row")
    quote_frame = quotes.copy().reset_index(drop=True)
    quote_frame["timestamp"] = _parse_explicit_utc(quote_frame["timestamp"], field_name="quotes")
    if quote_frame["timestamp"].duplicated().any():
        raise ValueError("quote timestamps must be unique")
    if not quote_frame["timestamp"].is_monotonic_increasing:
        raise ValueError("quote timestamps must be chronologically sorted")
    try:
        quote_prices = pd.to_numeric(quote_frame["price"], errors="raise").astype(float)
    except (TypeError, ValueError) as exc:
        raise ValueError("quote prices must be numeric and finite") from exc
    if quote_prices.isna().any() or not quote_prices.map(math.isfinite).all():
        raise ValueError("quote prices must be numeric and finite")
    if (quote_prices <= 0).any():
        raise ValueError("quote prices must be greater than zero")
    quote_frame["price"] = quote_prices

    quote_times = pd.DatetimeIndex(quote_frame["timestamp"])
    total_signals = int(signal_series.isin([CALL, PUT]).sum())
    counters = {
        "no_entry_quote": 0,
        "stale_entry_quote": 0,
        "no_settlement_quote": 0,
        "stale_settlement_quote": 0,
    }
    trades: list[dict[str, object]] = []
    for index, direction in enumerate(signal_series):
        if direction not in (CALL, PUT):
            continue

        bar_timestamp = pd.Timestamp(candles.at[index, "timestamp"])
        available_at = (
            bar_timestamp + pd.to_timedelta(bars_seconds, unit="s")
            if bar_timestamp_label == "open"
            else bar_timestamp
        )
        entry_target = available_at + pd.to_timedelta(entry_delay, unit="s")
        entry_index = int(quote_times.searchsorted(entry_target, side="left"))
        if entry_index >= len(quote_frame):
            counters["no_entry_quote"] += 1
            continue

        entry_row = quote_frame.iloc[entry_index]
        entry_time = pd.Timestamp(entry_row["timestamp"])
        entry_quote_delay = (entry_time - entry_target).total_seconds()
        if entry_quote_delay > max_delay:
            counters["stale_entry_quote"] += 1
            continue

        expiry_target = entry_time + pd.to_timedelta(expiry, unit="s")
        settlement_index = int(quote_times.searchsorted(expiry_target, side="left"))
        if settlement_index >= len(quote_frame):
            counters["no_settlement_quote"] += 1
            continue

        settlement_row = quote_frame.iloc[settlement_index]
        settlement_time = pd.Timestamp(settlement_row["timestamp"])
        settlement_delay = (settlement_time - expiry_target).total_seconds()
        if settlement_delay > max_delay:
            counters["stale_settlement_quote"] += 1
            continue

        entry_price = float(entry_row["price"])
        settlement_price = float(settlement_row["price"])
        if direction == CALL:
            outcome = "WIN" if settlement_price > entry_price else "LOSS" if settlement_price < entry_price else "TIE"
        else:
            outcome = "WIN" if settlement_price < entry_price else "LOSS" if settlement_price > entry_price else "TIE"

        if outcome == "WIN":
            pnl = stake_value * payout_value
        elif outcome == "TIE" and tie_policy == "push":
            pnl = 0.0
        else:
            if outcome == "TIE" and tie_policy == "loss":
                outcome = "LOSS"
            pnl = -stake_value

        trades.append({
            "signal_bar_timestamp": bar_timestamp,
            "signal_available_at": available_at,
            "direction": direction,
            "entry_target_timestamp": entry_target,
            "entry_timestamp": entry_time,
            "entry_quote_delay_seconds": entry_quote_delay,
            "entry_price": entry_price,
            "expiry_target_timestamp": expiry_target,
            "settlement_timestamp": settlement_time,
            "settlement_quote_delay_seconds": settlement_delay,
            "settlement_price": settlement_price,
            "outcome": outcome,
            "stake": stake_value,
            "payout": payout_value,
            "pnl": pnl,
        })

    columns = [
        "signal_bar_timestamp", "signal_available_at", "direction",
        "entry_target_timestamp", "entry_timestamp", "entry_quote_delay_seconds",
        "entry_price", "expiry_target_timestamp", "settlement_timestamp",
        "settlement_quote_delay_seconds", "settlement_price", "outcome",
        "stake", "payout", "pnl",
    ]
    results = pd.DataFrame(trades, columns=columns)
    results["cumulative_pnl"] = results["pnl"].cumsum()
    diagnostics: dict[str, int | float | str] = {
        "signal_candles": int(len(candles)),
        "candidate_signals": total_signals,
        "modeled_trades": int(len(results)),
        **counters,
        "bar_seconds": bars_seconds,
        "bar_timestamp_label": bar_timestamp_label,
        "expiry_seconds": expiry,
        "entry_delay_seconds": entry_delay,
        "max_quote_delay_seconds": max_delay,
        "payout": payout_value,
        "stake": stake_value,
        "tie_policy": tie_policy,
    }
    return results, diagnostics


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bpr02-seconds-backtest",
        description="Research-only second-expiry backtest using timestamped quote observations.",
    )
    parser.add_argument("candles_csv", help="OHLC CSV used for signals; timestamps must be timezone-aware")
    parser.add_argument("quotes_csv", help="Quote CSV with timezone-aware timestamp and price columns")
    parser.add_argument("--lookback", type=int, default=12)
    parser.add_argument("--bar-seconds", type=float, default=60.0)
    parser.add_argument("--bar-timestamp-label", choices=("open", "close"), default="open")
    parser.add_argument("--expiry-seconds", type=float, default=3.0)
    parser.add_argument("--entry-delay-seconds", type=float, default=0.0)
    parser.add_argument("--max-quote-delay-seconds", type=float, default=1.0)
    parser.add_argument("--payout", type=float, default=0.80)
    parser.add_argument("--stake", type=float, default=1.0)
    parser.add_argument("--tie-policy", choices=("push", "loss"), default="push")
    parser.add_argument("--output", default="bpr02_seconds_results.csv")
    parser.add_argument("--manifest-output", default="bpr02_seconds_run_manifest.json")
    parser.add_argument(
        "--quote-source-label",
        default="unverified_quote_feed",
        help="Descriptive feed label for the manifest; not independently verified",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    candles_path = Path(args.candles_csv)
    quotes_path = Path(args.quotes_csv)
    output_path = Path(args.output)
    manifest_path = Path(args.manifest_output)
    protected_inputs = {candles_path.resolve(), quotes_path.resolve()}
    if candles_path.resolve() == quotes_path.resolve():
        print("ERROR: candles_csv and quotes_csv must be different files", file=sys.stderr)
        return 2
    if output_path.resolve() in protected_inputs:
        print("ERROR: output path must not overwrite either input CSV", file=sys.stderr)
        return 2
    if manifest_path.resolve() in protected_inputs or manifest_path.resolve() == output_path.resolve():
        print("ERROR: manifest path must differ from both input CSVs and the output CSV", file=sys.stderr)
        return 2

    try:
        if args.lookback < 2:
            raise ValueError("--lookback must be >= 2")
        candles = load_ohlc_csv(candles_path)
        quotes = load_quote_csv(quotes_path)
        signal_frame = sweep_reclaim_signals(candles, lookback=args.lookback)
        trades, diagnostics = backtest_seconds_expiry(
            candles,
            signal_frame,
            quotes,
            bar_seconds=args.bar_seconds,
            bar_timestamp_label=args.bar_timestamp_label,
            expiry_seconds=args.expiry_seconds,
            entry_delay_seconds=args.entry_delay_seconds,
            max_quote_delay_seconds=args.max_quote_delay_seconds,
            payout=args.payout,
            stake=args.stake,
            tie_policy=args.tie_policy,
        )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        trades.to_csv(output_path, index=False)
        stats = summarize_results(trades)
        manifest = build_run_manifest(
            source_path=candles_path,
            additional_sources={"quotes": quotes_path},
            mode="seconds_expiry_research_only",
            parameters={
                "lookback": args.lookback,
                "bar_seconds": args.bar_seconds,
                "bar_timestamp_label": args.bar_timestamp_label,
                "expiry_seconds": args.expiry_seconds,
                "entry_delay_seconds": args.entry_delay_seconds,
                "max_quote_delay_seconds": args.max_quote_delay_seconds,
                "payout": args.payout,
                "stake": args.stake,
                "tie_policy": args.tie_policy,
                "quote_source_label": args.quote_source_label,
            },
            outputs={"trades_csv": str(output_path)},
            rows=len(candles),
            start_timestamp=str(candles["timestamp"].iloc[0]),
            end_timestamp=str(candles["timestamp"].iloc[-1]),
            summary={**diagnostics, **stats},
            notes=(
                "This simulation does not send or modify orders.",
                "The quote-source label is user supplied and not independently verified.",
                "A conventional EUR/USD quote series does not establish parity with Pocket Option EUR/USD OTC.",
                "Results depend on the configured quote-delay tolerance and settlement-price convention.",
                "This is historical research, not proof of profitability.",
            ),
        )
        write_run_manifest(manifest_path, manifest, source_path=candles_path)
        result = {
            "mode": "seconds_expiry_research_only",
            "trades_file": str(output_path),
            "manifest_file": str(manifest_path),
            **diagnostics,
            **stats,
            "orders_submitted": 0,
            "warning": (
                "Second-expiry results require quotes from the intended market and price convention; "
                "external EUR/USD data may not match Pocket Option OTC."
            ),
        }
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

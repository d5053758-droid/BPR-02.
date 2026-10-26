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
    """Vectorized timestamp parser that refuses timezone-naive source values."""
    missing = values.isna().to_numpy()
    if missing.any():
        row_number = int(next(index for index, is_missing in enumerate(missing, start=1) if is_missing))
        raise ValueError(f"{field_name} has a missing timestamp at row {row_number}")

    rendered = values.astype("string").str.strip()
    has_timezone = rendered.str.contains(
        r"(?:[zZ]|[+-]\d{2}:?\d{2})$",
        regex=True,
        na=False,
    )
    invalid_timezone = ~has_timezone.to_numpy()
    if invalid_timezone.any():
        row_number = int(next(index for index, invalid in enumerate(invalid_timezone, start=1) if invalid))
        raise ValueError(
            f"{field_name} timestamps must include a timezone/UTC offset; "
            f"row {row_number} is timezone-naive"
        )

    try:
        parsed = pd.to_datetime(values, errors="raise", utc=True, format="mixed")
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{field_name} contains an invalid timestamp: {exc}") from exc
    if parsed.isna().any():
        raise ValueError(f"{field_name} timestamps must not be missing")
    return pd.Series(parsed, index=values.index, name="timestamp")


def _require_explicit_timezone_in_ohlc_csv(path: str | Path) -> None:
    """Reject naive source timestamps before the generic CSV loader normalizes them."""
    source = Path(path)
    try:
        header = pd.read_csv(source, nrows=0)
    except Exception as exc:
        raise ValueError(f"could not read signal candle CSV header: {exc}") from exc
    columns = {str(column).strip().lower(): column for column in header.columns}
    timestamp_column = next(
        (columns[alias] for alias in ("timestamp", "datetime", "date_time", "date", "time") if alias in columns),
        None,
    )
    if timestamp_column is None:
        raise ValueError("signal candle CSV must have a timestamp column with an explicit timezone/UTC offset")
    try:
        raw_timestamps = pd.read_csv(source, usecols=[timestamp_column], dtype=str)[timestamp_column]
    except Exception as exc:
        raise ValueError(f"could not read signal candle timestamps: {exc}") from exc
    _parse_explicit_utc(raw_timestamps, field_name="signal candle CSV")


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




def _validate_quote_frame(quotes: pd.DataFrame) -> pd.DataFrame:
    """Validate and normalize quote observations once at a reusable boundary."""
    required = {"timestamp", "price"}
    missing = sorted(required.difference(quotes.columns))
    if missing:
        raise ValueError(f"quotes must contain {sorted(required)}; missing {missing}")
    if quotes.empty:
        raise ValueError("quotes must contain at least one row")
    quote_frame = quotes.copy().reset_index(drop=True)
    quote_frame["timestamp"] = _parse_explicit_utc(
        quote_frame["timestamp"], field_name="quotes"
    )
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
    return pd.DataFrame({"timestamp": quote_frame["timestamp"], "price": quote_prices})


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
    quote_frame = _validate_quote_frame(quotes)

    quote_times = pd.DatetimeIndex(quote_frame["timestamp"])
    quote_intervals = quote_times.to_series().diff().dt.total_seconds().dropna()
    quote_span_seconds = (
        float((quote_times[-1] - quote_times[0]).total_seconds())
        if len(quote_times) >= 2
        else 0.0
    )
    quote_gap_median_seconds = (
        float(quote_intervals.median()) if not quote_intervals.empty else None
    )
    quote_gap_p95_seconds = (
        float(quote_intervals.quantile(0.95)) if not quote_intervals.empty else None
    )
    quote_gap_max_seconds = (
        float(quote_intervals.max()) if not quote_intervals.empty else None
    )
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
    unmodeled_signals = total_signals - int(len(results))
    diagnostics: dict[str, int | float | str | None] = {
        "signal_candles": int(len(candles)),
        "candidate_signals": total_signals,
        "modeled_trades": int(len(results)),
        "unmodeled_signals": unmodeled_signals,
        "modeled_signal_fraction": (len(results) / total_signals) if total_signals else None,
        "quote_observations": int(len(quote_frame)),
        "quote_start_timestamp": quote_times[0].isoformat(),
        "quote_end_timestamp": quote_times[-1].isoformat(),
        "quote_span_seconds": quote_span_seconds,
        "quote_gap_median_seconds": quote_gap_median_seconds,
        "quote_gap_p95_seconds": quote_gap_p95_seconds,
        "quote_gap_max_seconds": quote_gap_max_seconds,
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



def run_seconds_walk_forward(
    signal_candles: pd.DataFrame,
    signals: pd.Series | pd.DataFrame,
    quotes: pd.DataFrame,
    *,
    initial_train_size: int,
    test_size: int,
    step_size: int | None = None,
    bar_seconds: float = 60.0,
    bar_timestamp_label: BarTimestampLabel = "open",
    expiry_seconds: float = 3.0,
    entry_delay_seconds: float = 0.0,
    max_quote_delay_seconds: float = 1.0,
    payout: float = 0.80,
    stake: float = 1.0,
    tie_policy: TiePolicy = "push",
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, int | float | str | None]]:
    """Evaluate a fixed second-expiry rule on future-only, non-overlapping folds.

    The strategy rule is not fitted or optimized within folds. The earlier rows
    supply chronological context; each reported fold contains signals from a
    strictly later test block. Signals must be computed causally from current
    and past candles only. Quotes are sliced per fold, with one later observation
    retained to classify a missing timely quote as stale instead of fabricating
    a fill.

    Returns (out-of-sample trades, fold metrics, aggregate diagnostics). This
    function is research-only and does not contact a broker or submit orders.
    """
    from .evaluation import expanding_window_splits

    if step_size is not None and step_size < test_size:
        raise ValueError("step_size must be >= test_size to avoid overlapping test folds")
    candles, series = _validate_candles_and_signals(signal_candles, signals)
    quote_frame = _validate_quote_frame(quotes)
    timestamps = pd.to_datetime(candles["timestamp"], errors="raise", utc=True)
    splits = expanding_window_splits(
        len(candles),
        initial_train_size=initial_train_size,
        test_size=test_size,
        step_size=step_size,
    )
    bars_seconds = _as_finite_number(bar_seconds, "bar_seconds", minimum=0, inclusive=False)
    expiry = _as_finite_number(expiry_seconds, "expiry_seconds", minimum=0, inclusive=False)
    entry_delay = _as_finite_number(
        entry_delay_seconds, "entry_delay_seconds", minimum=0, inclusive=True
    )
    max_delay = _as_finite_number(
        max_quote_delay_seconds, "max_quote_delay_seconds", minimum=0, inclusive=True
    )
    if bar_timestamp_label not in ("open", "close"):
        raise ValueError("bar_timestamp_label must be 'open' or 'close'")
    if isinstance(payout, bool) or not isinstance(payout, (int, float)) or not math.isfinite(float(payout)) or not 0 <= float(payout) <= 1:
        raise ValueError("payout must be a finite number between 0 and 1")
    stake_value = _as_finite_number(stake, "stake", minimum=0, inclusive=False)
    if tie_policy not in ("push", "loss"):
        raise ValueError("tie_policy must be 'push' or 'loss'")

    quote_times = pd.DatetimeIndex(quote_frame["timestamp"])
    trade_tables: list[pd.DataFrame] = []
    metric_rows: list[dict[str, object]] = []
    counter_names = (
        "no_entry_quote",
        "stale_entry_quote",
        "no_settlement_quote",
        "stale_settlement_quote",
    )

    for fold_number, (train_slice, test_slice) in enumerate(splits, start=1):
        test_candles = candles.iloc[test_slice].reset_index(drop=True)
        test_signals = series.iloc[test_slice].reset_index(drop=True)
        test_start = pd.Timestamp(test_candles["timestamp"].iloc[0])
        test_end = pd.Timestamp(test_candles["timestamp"].iloc[-1])
        availability_end = (
            test_end + pd.to_timedelta(bars_seconds, unit="s")
            if bar_timestamp_label == "open"
            else test_end
        )
        quote_end = (
            availability_end
            + pd.to_timedelta(entry_delay + expiry + max_delay, unit="s")
        )
        quote_start_index = int(quote_times.searchsorted(test_start, side="left"))
        quote_end_index = int(quote_times.searchsorted(quote_end, side="right"))
        if quote_end_index < len(quote_frame):
            quote_end_index += 1  # Keep one observation beyond the tolerance boundary as a stale-quote sentinel.
        quote_subset = quote_frame.iloc[quote_start_index:quote_end_index].reset_index(drop=True)

        if quote_subset.empty:
            candidate_count = int(test_signals.isin([CALL, PUT]).sum())
            empty_columns = [
                "signal_bar_timestamp", "signal_available_at", "direction",
                "entry_target_timestamp", "entry_timestamp", "entry_quote_delay_seconds",
                "entry_price", "expiry_target_timestamp", "settlement_timestamp",
                "settlement_quote_delay_seconds", "settlement_price", "outcome",
                "stake", "payout", "pnl", "cumulative_pnl",
            ]
            fold_trades = pd.DataFrame(columns=empty_columns)
            fold_diagnostics: dict[str, int | float | str | None] = {
                "signal_candles": len(test_candles),
                "candidate_signals": candidate_count,
                "modeled_trades": 0,
                "unmodeled_signals": candidate_count,
                "modeled_signal_fraction": 0.0 if candidate_count else None,
                "quote_observations": 0,
                "quote_start_timestamp": None,
                "quote_end_timestamp": None,
                "quote_span_seconds": 0.0,
                "quote_gap_median_seconds": None,
                "quote_gap_p95_seconds": None,
                "quote_gap_max_seconds": None,
                "no_entry_quote": candidate_count,
                "stale_entry_quote": 0,
                "no_settlement_quote": 0,
                "stale_settlement_quote": 0,
                "bar_seconds": bars_seconds,
                "bar_timestamp_label": bar_timestamp_label,
                "expiry_seconds": expiry,
                "entry_delay_seconds": entry_delay,
                "max_quote_delay_seconds": max_delay,
                "payout": float(payout),
                "stake": stake_value,
                "tie_policy": tie_policy,
            }
        else:
            fold_trades, fold_diagnostics = backtest_seconds_expiry(
                test_candles,
                test_signals,
                quote_subset,
                bar_seconds=bars_seconds,
                bar_timestamp_label=bar_timestamp_label,
                expiry_seconds=expiry,
                entry_delay_seconds=entry_delay,
                max_quote_delay_seconds=max_delay,
                payout=float(payout),
                stake=stake_value,
                tie_policy=tie_policy,
            )

        fold_trades = fold_trades.copy()
        fold_trades.insert(0, "fold", fold_number)
        trade_tables.append(fold_trades)
        metrics = summarize_results(fold_trades)
        metric_rows.append({
            "fold": fold_number,
            "training_rows": train_slice.stop,
            "test_rows": test_slice.stop - test_slice.start,
            "training_end": timestamps.iloc[train_slice.stop - 1],
            "test_start": test_start,
            "test_end": test_end,
            **fold_diagnostics,
            **metrics,
        })

    all_trades = (
        pd.concat(trade_tables, ignore_index=True)
        if trade_tables
        else pd.DataFrame()
    )
    fold_metrics = pd.DataFrame(metric_rows)
    total_candidates = int(fold_metrics["candidate_signals"].sum())
    modeled_trades = int(len(all_trades))
    aggregate_counters = {
        name: int(fold_metrics[name].sum())
        for name in counter_names
    }
    global_intervals = quote_times.to_series().diff().dt.total_seconds().dropna()
    quote_span = (
        float((quote_times[-1] - quote_times[0]).total_seconds())
        if len(quote_times) >= 2
        else 0.0
    )
    aggregate_diagnostics: dict[str, int | float | str | None] = {
        "signal_candles": int(fold_metrics["test_rows"].sum()),
        "candidate_signals": total_candidates,
        "modeled_trades": modeled_trades,
        "unmodeled_signals": total_candidates - modeled_trades,
        "modeled_signal_fraction": modeled_trades / total_candidates if total_candidates else None,
        "quote_observations": len(quote_frame),
        "quote_start_timestamp": quote_times[0].isoformat(),
        "quote_end_timestamp": quote_times[-1].isoformat(),
        "quote_span_seconds": quote_span,
        "quote_gap_median_seconds": float(global_intervals.median()) if not global_intervals.empty else None,
        "quote_gap_p95_seconds": float(global_intervals.quantile(0.95)) if not global_intervals.empty else None,
        "quote_gap_max_seconds": float(global_intervals.max()) if not global_intervals.empty else None,
        **aggregate_counters,
        "bar_seconds": bars_seconds,
        "bar_timestamp_label": bar_timestamp_label,
        "expiry_seconds": expiry,
        "entry_delay_seconds": entry_delay,
        "max_quote_delay_seconds": max_delay,
        "payout": float(payout),
        "stake": stake_value,
        "tie_policy": tie_policy,
        "walk_forward_folds": len(fold_metrics),
        "initial_train_size": initial_train_size,
        "test_size": test_size,
        "step_size": test_size if step_size is None else step_size,
        "orders_submitted": 0,
    }
    return all_trades, fold_metrics, aggregate_diagnostics

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
    parser.add_argument(
        "--min-model-coverage",
        type=float,
        default=0.95,
        help="Minimum share of candidate signals with modeled entry and settlement quotes; exits 3 if unmet",
    )
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
    parser.add_argument(
        "--walk-forward",
        action="store_true",
        help="Evaluate the fixed signal rule on expanding chronological test windows",
    )
    parser.add_argument(
        "--initial-train-size",
        type=int,
        help="Number of earlier candles before the first test window; required with --walk-forward",
    )
    parser.add_argument(
        "--test-size",
        type=int,
        help="Number of candles in each future-only test window; required with --walk-forward",
    )
    parser.add_argument(
        "--step-size",
        type=int,
        help="Rows to advance between test windows (must be >= test size; defaults to test size)",
    )
    parser.add_argument(
        "--fold-summary",
        default="bpr02_seconds_fold_metrics.csv",
        help="Output CSV for per-fold out-of-sample metrics",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    candles_path = Path(args.candles_csv)
    quotes_path = Path(args.quotes_csv)
    output_path = Path(args.output)
    manifest_path = Path(args.manifest_output)
    fold_summary_path = Path(args.fold_summary)

    input_paths = {candles_path.resolve(), quotes_path.resolve()}
    if candles_path.resolve() == quotes_path.resolve():
        print("ERROR: candles_csv and quotes_csv must be different files", file=sys.stderr)
        return 2

    output_paths = [output_path.resolve(), manifest_path.resolve()]
    if args.walk_forward:
        output_paths.append(fold_summary_path.resolve())
    if any(path in input_paths for path in output_paths):
        print("ERROR: output paths must not overwrite either input CSV", file=sys.stderr)
        return 2
    if len(set(output_paths)) != len(output_paths):
        print("ERROR: trades, manifest, and fold-summary output paths must be different", file=sys.stderr)
        return 2

    try:
        if args.lookback < 2:
            raise ValueError("--lookback must be >= 2")
        if not isinstance(args.quote_source_label, str) or not args.quote_source_label.strip():
            raise ValueError("--quote-source-label must be a non-empty descriptive label")
        if not math.isfinite(args.min_model_coverage) or not 0 <= args.min_model_coverage <= 1:
            raise ValueError("--min-model-coverage must be between 0 and 1")
        if args.walk_forward and (
            args.initial_train_size is None or args.test_size is None
        ):
            raise ValueError("--walk-forward requires --initial-train-size and --test-size")

        _require_explicit_timezone_in_ohlc_csv(candles_path)
        candles = load_ohlc_csv(candles_path)
        quotes = load_quote_csv(quotes_path)
        signal_frame = sweep_reclaim_signals(candles, lookback=args.lookback)

        base_parameters: dict[str, object] = {
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
            "min_model_coverage": args.min_model_coverage,
        }

        if args.walk_forward:
            trades, fold_metrics, diagnostics = run_seconds_walk_forward(
                candles,
                signal_frame,
                quotes,
                initial_train_size=args.initial_train_size,
                test_size=args.test_size,
                step_size=args.step_size,
                bar_seconds=args.bar_seconds,
                bar_timestamp_label=args.bar_timestamp_label,
                expiry_seconds=args.expiry_seconds,
                entry_delay_seconds=args.entry_delay_seconds,
                max_quote_delay_seconds=args.max_quote_delay_seconds,
                payout=args.payout,
                stake=args.stake,
                tie_policy=args.tie_policy,
            )
            fold_summary_path.parent.mkdir(parents=True, exist_ok=True)
            fold_metrics.to_csv(fold_summary_path, index=False)
            mode = "seconds_expiry_walk_forward_research_only"
            base_parameters.update({
                "walk_forward": True,
                "initial_train_size": args.initial_train_size,
                "test_size": args.test_size,
                "step_size": args.test_size if args.step_size is None else args.step_size,
            })
            output_map = {
                "trades_csv": str(output_path),
                "fold_summary_csv": str(fold_summary_path),
            }
            extra_result: dict[str, object] = {
                "fold_summary_file": str(fold_summary_path),
                "folds": int(len(fold_metrics)),
            }
            manifest_notes = (
                "Only signals within future-only test folds are included in the reported trades.",
                "The fixed sweep/reclaim rule is not fitted or optimized within folds.",
                "This simulation does not send or modify orders.",
                "The quote-source label is user supplied and not independently verified.",
                "A conventional EUR/USD quote series does not establish parity with Pocket Option EUR/USD OTC.",
                "Quote selection uses the first observation at or after target times, within the configured delay tolerance.",
                "This is historical research, not proof of profitability.",
            )
        else:
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
            mode = "seconds_expiry_research_only"
            output_map = {"trades_csv": str(output_path)}
            extra_result = {}
            manifest_notes = (
                "This simulation does not send or modify orders.",
                "The quote-source label is user supplied and not independently verified.",
                "A conventional EUR/USD quote series does not establish parity with Pocket Option EUR/USD OTC.",
                "Results depend on the configured quote-delay tolerance and settlement-price convention.",
                "This is historical research, not proof of profitability.",
            )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        trades.to_csv(output_path, index=False)
        stats = summarize_results(trades)
        manifest = build_run_manifest(
            source_path=candles_path,
            additional_sources={"quotes": quotes_path},
            mode=mode,
            parameters=base_parameters,
            outputs=output_map,
            rows=len(candles),
            start_timestamp=str(candles["timestamp"].iloc[0]),
            end_timestamp=str(candles["timestamp"].iloc[-1]),
            summary={**diagnostics, **stats},
            notes=manifest_notes,
        )
        model_coverage = diagnostics.get("modeled_signal_fraction")
        candidate_count = int(diagnostics.get("candidate_signals", 0))
        model_coverage_gate_passed = (
            candidate_count > 0
            and model_coverage is not None
            and float(model_coverage) >= args.min_model_coverage
        )
        manifest["parameters"]["min_model_coverage"] = args.min_model_coverage
        manifest["summary"]["model_coverage_gate_passed"] = model_coverage_gate_passed
        manifest["summary"]["minimum_model_coverage"] = args.min_model_coverage
        write_run_manifest(manifest_path, manifest, source_path=candles_path)
        result = {
            "mode": mode,
            "trades_file": str(output_path),
            "manifest_file": str(manifest_path),
            **diagnostics,
            **stats,
            **extra_result,
            "model_coverage_gate_passed": model_coverage_gate_passed,
            "minimum_model_coverage": args.min_model_coverage,
            "orders_submitted": 0,
            "warning": (
                "Second-expiry results require quotes from the intended market and price convention; "
                "external EUR/USD data may not match Pocket Option OTC."
            ) if model_coverage_gate_passed else (
                "MODEL COVERAGE GATE FAILED: too few candidate signals had usable entry and settlement quotes. "
                "Treat win rate and P/L as incomplete; inspect unmodeled signals and quote gaps."
            ),
        }
        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
        if not model_coverage_gate_passed:
            print(
                f"MODEL COVERAGE THRESHOLD NOT MET: modeled {model_coverage!r} of candidate signals; "
                f"minimum required is {args.min_model_coverage:.3f}",
                file=sys.stderr,
            )
            return 3
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

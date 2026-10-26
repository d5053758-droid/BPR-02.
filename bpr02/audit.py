"""Historical candle quality checks intended to run before strategy evaluation."""
from __future__ import annotations

from collections.abc import Mapping
from math import isfinite

import pandas as pd


def audit_candles(
    candles: pd.DataFrame,
    *,
    expected_interval_seconds: float | None = None,
    max_gap_examples: int = 20,
) -> dict[str, object]:
    """Report timestamp, candle-range and missing-interval diagnostics.

    When no expected interval is supplied, the most common positive observed
    interval is inferred; ties prefer the shorter interval. Larger gaps are
    estimated from the expected interval and are explicitly marked as
    estimates. A structural-validity result does not verify that these prices
    match Pocket Option OTC or any particular broker feed.
    """
    if max_gap_examples < 0:
        raise ValueError("max_gap_examples must be >= 0")
    required = {"timestamp", "open", "high", "low", "close"}
    missing_columns = sorted(required.difference(candles.columns))
    if missing_columns:
        raise ValueError(f"candles missing columns: {missing_columns}")
    if expected_interval_seconds is not None and (
        not isfinite(expected_interval_seconds) or expected_interval_seconds <= 0
    ):
        raise ValueError("expected_interval_seconds must be a finite value > 0")

    frame = candles.copy()
    original_count = len(frame)
    timestamps = pd.to_datetime(frame["timestamp"], errors="coerce", utc=True)
    bad_timestamps = int(timestamps.isna().sum())
    valid_ts = timestamps.dropna()
    duplicate_count = int(valid_ts.duplicated().sum())
    out_of_order_count = int((valid_ts.diff().dt.total_seconds().dropna() < 0).sum())

    numeric: dict[str, pd.Series] = {
        column: pd.to_numeric(frame[column], errors="coerce")
        for column in ("open", "high", "low", "close")
    }
    finite = pd.Series(True, index=frame.index)
    for values in numeric.values():
        finite &= values.notna() & values.map(lambda x: pd.notna(x) and abs(float(x)) != float("inf"))
    bad_high = numeric["high"] < pd.concat(
        [numeric["open"], numeric["close"], numeric["low"]], axis=1
    ).max(axis=1)
    bad_low = numeric["low"] > pd.concat(
        [numeric["open"], numeric["close"], numeric["high"]], axis=1
    ).min(axis=1)
    invalid_ohlc_count = int((~finite | bad_high | bad_low).sum())

    deltas = valid_ts.diff().dt.total_seconds().dropna()
    positive_deltas = deltas[deltas > 0]
    inferred_interval: float | None = None
    if not positive_deltas.empty:
        counts = positive_deltas.value_counts()
        # Stable tie-break: prefer the shortest positive interval.
        most_common = counts[counts == counts.max()].index.tolist()
        inferred_interval = float(min(most_common))

    interval = (
        float(expected_interval_seconds)
        if expected_interval_seconds is not None
        else inferred_interval
    )
    interval_source = "provided" if expected_interval_seconds is not None else "inferred"

    gap_examples: list[Mapping[str, object]] = []
    gap_count = 0
    estimated_missing_bars = 0
    irregular_interval_count = 0
    if interval is not None and interval > 0 and len(valid_ts) > 1:
        for i in range(1, len(valid_ts)):
            previous = valid_ts.iloc[i - 1]
            current = valid_ts.iloc[i]
            seconds = float((current - previous).total_seconds())
            if seconds <= 0:
                continue
            if abs(seconds - interval) > 1e-9:
                irregular_interval_count += 1
            if seconds > interval * 1.5:
                estimated = max(int(round(seconds / interval)) - 1, 0)
                gap_count += 1
                estimated_missing_bars += estimated
                if len(gap_examples) < max_gap_examples:
                    gap_examples.append({
                        "after": previous.isoformat(),
                        "before": current.isoformat(),
                        "gap_seconds": seconds,
                        "estimated_missing_bars": estimated,
                    })

    structurally_valid = (
        original_count > 0
        and bad_timestamps == 0
        and duplicate_count == 0
        and out_of_order_count == 0
        and invalid_ohlc_count == 0
    )
    return {
        "rows": original_count,
        "start_timestamp": valid_ts.iloc[0].isoformat() if not valid_ts.empty else None,
        "end_timestamp": valid_ts.iloc[-1].isoformat() if not valid_ts.empty else None,
        "timestamp_parse_errors": bad_timestamps,
        "duplicate_timestamps": duplicate_count,
        "out_of_order_steps": out_of_order_count,
        "invalid_ohlc_rows": invalid_ohlc_count,
        "expected_interval_seconds": interval,
        "interval_source": interval_source if interval is not None else "unavailable",
        "irregular_intervals": irregular_interval_count,
        "gaps_over_1_5x_interval": gap_count,
        "estimated_missing_bars": estimated_missing_bars,
        "gap_examples": gap_examples,
        "structurally_valid": structurally_valid,
        "notes": [
            "Gap counts depend on the supplied or inferred interval.",
            "Missing bars are estimates; market closures and feed behavior can explain gaps.",
            "This report does not verify parity with Pocket Option OTC prices or settlement.",
        ],
    }

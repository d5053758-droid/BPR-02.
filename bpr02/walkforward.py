"""Walk-forward orchestration for the experimental sweep/reclaim baseline."""
from __future__ import annotations

import pandas as pd

from .backtest import backtest_coverage_diagnostics, backtest_fixed_expiry, summarize_results
from .evaluation import expanding_window_splits
from .signals import CALL, PUT, sweep_reclaim_signals


def run_walk_forward(
    candles: pd.DataFrame,
    *,
    initial_train_size: int,
    test_size: int,
    lookback: int = 12,
    expiry_bars: int = 1,
    expected_interval_seconds: float | None = None,
    payout: float = 0.80,
    stake: float = 1.0,
    tie_policy: str = "push",
    step_size: int | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Run fixed-expiry simulations in successive future-only test windows.

    Signal references are calculated with past candles only, using the full
    chronological series; each fold's trades are restricted to signals and
    entry/expiry candles inside that fold's test block. The training segment
    establishes historical context only: no model is fitted or tuned here.
    Returns (trade_results, fold_metrics). Timestamp discontinuities reset
    signal context and disqualify trades whose modeled candle window crosses
    a gap. The expected candle interval may be supplied explicitly. This is a
    validation utility, not evidence of strategy profitability.
    """
    required = {"timestamp", "open", "high", "low", "close"}
    if not required.issubset(candles.columns):
        raise ValueError(f"candles must contain {sorted(required)}")
    timestamps = pd.to_datetime(candles["timestamp"], errors="raise", utc=True)
    if timestamps.isna().any() or timestamps.duplicated().any():
        raise ValueError("timestamps must be present and unique")
    if not timestamps.is_monotonic_increasing:
        raise ValueError("candles must be chronologically sorted")

    # Validate the window parameters before constructing the signal series.
    splits = expanding_window_splits(
        len(candles),
        initial_train_size=initial_train_size,
        test_size=test_size,
        step_size=step_size,
    )
    signals = sweep_reclaim_signals(
        candles, lookback=lookback,
        expected_interval_seconds=expected_interval_seconds,
    )
    trade_tables: list[pd.DataFrame] = []
    metric_rows: list[dict[str, object]] = []

    for fold, (train_slice, test_slice) in enumerate(splits, start=1):
        test_candles = candles.iloc[test_slice].reset_index(drop=True)
        test_signals = signals.iloc[test_slice].reset_index(drop=True)
        trades = backtest_fixed_expiry(
            test_candles,
            test_signals,
            expiry_bars=expiry_bars,
            expected_interval_seconds=expected_interval_seconds,
            payout=payout,
            stake=stake,
            tie_policy=tie_policy,
        )
        coverage = backtest_coverage_diagnostics(
            test_candles,
            test_signals,
            expiry_bars=expiry_bars,
            expected_interval_seconds=expected_interval_seconds,
        )
        trades.insert(0, "fold", fold)
        trade_tables.append(trades)

        metric_rows.append({
            "fold": fold,
            "training_rows": train_slice.stop,
            "test_rows": test_slice.stop - test_slice.start,
            "training_end": timestamps.iloc[train_slice.stop - 1],
            "test_start": timestamps.iloc[test_slice.start],
            "test_end": timestamps.iloc[test_slice.stop - 1],
            **coverage,
            **summarize_results(trades),
        })

    all_trades = pd.concat(trade_tables, ignore_index=True) if trade_tables else pd.DataFrame()
    fold_metrics = pd.DataFrame(metric_rows)
    return all_trades, fold_metrics

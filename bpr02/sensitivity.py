"""Grid sensitivity checks split chronologically into train and test segments."""
from __future__ import annotations

from collections.abc import Iterable

import pandas as pd

from .backtest import backtest_fixed_expiry, summarize_results
from .evaluation import chronological_split
from .signals import CALL, PUT, sweep_reclaim_signals


def run_sensitivity_analysis(
    candles: pd.DataFrame,
    *,
    lookbacks: Iterable[int] = (6, 12, 18),
    expiry_bars: Iterable[int] = (1, 2, 3),
    expected_interval_seconds: float | None = None,
    train_fraction: float = 0.70,
    payout: float = 0.80,
    stake: float = 1.0,
    tie_policy: str = "push",
) -> pd.DataFrame:
    """Compare lookback/expiry combinations on separate chronological segments.

    Signals are computed using only each candle and its past. The dataset is
    split chronologically; training trades stay inside the training segment,
    while test trades stay inside the later test segment. Test signals can use
    available earlier candles as lookback context. The grid is descriptive:
    it neither selects a winning configuration nor establishes future edge.
    """
    lookback_values = tuple(lookbacks)
    expiry_values = tuple(expiry_bars)
    if not lookback_values or not expiry_values:
        raise ValueError("lookbacks and expiry_bars must both contain values")
    if any(not isinstance(v, int) or isinstance(v, bool) or v < 2 for v in lookback_values):
        raise ValueError("every lookback must be an integer >= 2")
    if any(not isinstance(v, int) or isinstance(v, bool) or v < 1 for v in expiry_values):
        raise ValueError("every expiry_bars value must be an integer >= 1")
    if len(set(lookback_values)) != len(lookback_values):
        raise ValueError("lookbacks must not contain duplicates")
    if len(set(expiry_values)) != len(expiry_values):
        raise ValueError("expiry_bars must not contain duplicates")

    train, test = chronological_split(candles, train_fraction=train_fraction)
    split_index = len(train)
    rows: list[dict[str, object]] = []

    for lookback in lookback_values:
        signals = sweep_reclaim_signals(
            candles, lookback=lookback,
            expected_interval_seconds=expected_interval_seconds,
        )
        for segment, segment_candles, segment_signals in (
            ("train", train, signals.iloc[:split_index]),
            ("test", test, signals.iloc[split_index:]),
        ):
            local_signals = segment_signals.reset_index(drop=True)
            local_candles = segment_candles.reset_index(drop=True)
            for expiry in expiry_values:
                trades = backtest_fixed_expiry(
                    local_candles,
                    local_signals,
                    expiry_bars=expiry,
                    expected_interval_seconds=expected_interval_seconds,
                    payout=payout,
                    stake=stake,
                    tie_policy=tie_policy,
                )
                rows.append({
                    "segment": segment,
                    "lookback": lookback,
                    "expiry_bars": expiry,
                    "candidate_signals": int(local_signals["signal"].isin([CALL, PUT]).sum()),
                    **summarize_results(trades),
                })
    return pd.DataFrame(rows)

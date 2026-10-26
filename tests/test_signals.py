import pandas as pd
import pytest

from bpr02.signals import CALL, NO_TRADE, PUT, sweep_reclaim_signals


def test_sweep_reclaim_uses_only_prior_candles():
    frame = pd.DataFrame({
        "open":  [10, 10, 10, 8.5, 10],
        "high":  [11, 11, 11, 9.8, 12],
        "low":   [9, 9.5, 9.2, 8, 9.5],
        "close": [10, 10.5, 10, 8.8, 10.5],
    })
    result = sweep_reclaim_signals(frame, lookback=3)
    assert result["signal"].iloc[:3].tolist() == [NO_TRADE] * 3
    assert result["signal"].iloc[3] == NO_TRADE
    assert result["signal"].iloc[4] == PUT


def test_invalid_lookback_rejected():
    frame = pd.DataFrame({"open": [1], "high": [2], "low": [0], "close": [1]})
    with pytest.raises(ValueError):
        sweep_reclaim_signals(frame, lookback=1)


def test_explicit_expected_interval_detects_systematically_missing_bars():
    timestamps = pd.to_datetime([
        "2025-01-01T00:00:00Z",
        "2025-01-01T00:02:00Z",
        "2025-01-01T00:04:00Z",
        "2025-01-01T00:06:00Z",
    ])
    frame = pd.DataFrame({
        "timestamp": timestamps,
        "open": [10.0, 10.0, 10.0, 9.5],
        "high": [11.0, 11.0, 11.0, 10.5],
        "low": [9.0, 9.0, 9.0, 8.0],
        "close": [10.0, 10.0, 10.0, 10.0],
    })
    inferred = sweep_reclaim_signals(frame, lookback=3)
    explicit = sweep_reclaim_signals(
        frame, lookback=3, expected_interval_seconds=60
    )
    assert inferred["signal"].iloc[3] == CALL
    assert explicit["signal"].iloc[3] == NO_TRADE

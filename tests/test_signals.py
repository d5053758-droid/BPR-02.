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

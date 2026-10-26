"""Small, explicit technical indicator implementations."""
from __future__ import annotations

import pandas as pd


def ema(values: pd.Series, period: int) -> pd.Series:
    """Return exponential moving average; initial values use complete history."""
    if period < 1:
        raise ValueError("period must be >= 1")
    numeric = pd.to_numeric(values, errors="coerce")
    return numeric.ewm(span=period, adjust=False, min_periods=period).mean()


def atr(candles: pd.DataFrame, period: int = 14) -> pd.Series:
    """Return simple-moving-average ATR from OHLC candles.

    This uses a simple rolling mean of true range, not Wilder smoothing.
    The initial period - 1 values are NaN.
    """
    if period < 1:
        raise ValueError("period must be >= 1")
    required = {"high", "low", "close"}
    if not required.issubset(candles.columns):
        raise ValueError(f"candles must contain {sorted(required)}")
    high = pd.to_numeric(candles["high"], errors="coerce")
    low = pd.to_numeric(candles["low"], errors="coerce")
    close = pd.to_numeric(candles["close"], errors="coerce")
    previous_close = close.shift(1)
    true_range = pd.concat(
        [high - low, (high - previous_close).abs(), (low - previous_close).abs()],
        axis=1,
    ).max(axis=1, skipna=True)
    return true_range.rolling(window=period, min_periods=period).mean()

"""Experimental candle-close signal rules. These are hypotheses, not advice."""
from __future__ import annotations

import pandas as pd

CALL = "CALL"
PUT = "PUT"
NO_TRADE = "NO TRADE"


def sweep_reclaim_signals(candles: pd.DataFrame, lookback: int = 12) -> pd.DataFrame:
    """Detect a sweep beyond the prior range followed by a close back inside.

    A CALL candidate occurs when current low breaks the minimum low of the
    preceding lookback candles and current close finishes above that prior low.
    PUT mirrors this at the prior maximum. The current candle is excluded from
    the reference range with shift(1), preventing future-candle lookahead.
    Signal is known only at candle close; entry must be no earlier than the
    next candle open.
    """
    if lookback < 2:
        raise ValueError("lookback must be >= 2")
    required = {"open", "high", "low", "close"}
    if not required.issubset(candles.columns):
        raise ValueError(f"candles must contain {sorted(required)}")
    frame = candles.copy()
    prior_low = pd.to_numeric(frame["low"], errors="coerce").rolling(
        lookback, min_periods=lookback
    ).min().shift(1)
    prior_high = pd.to_numeric(frame["high"], errors="coerce").rolling(
        lookback, min_periods=lookback
    ).max().shift(1)
    current_low = pd.to_numeric(frame["low"], errors="coerce")
    current_high = pd.to_numeric(frame["high"], errors="coerce")
    close = pd.to_numeric(frame["close"], errors="coerce")
    call = (current_low < prior_low) & (close > prior_low)
    put = (current_high > prior_high) & (close < prior_high)
    labels = pd.Series(NO_TRADE, index=frame.index, dtype="object")
    labels.loc[call & ~put] = CALL
    labels.loc[put & ~call] = PUT
    reasons = pd.Series("No complete sweep-and-reclaim setup", index=frame.index, dtype="object")
    reasons.loc[call & ~put] = "Low swept prior range and candle closed above prior low"
    reasons.loc[put & ~call] = "High swept prior range and candle closed below prior high"
    reasons.loc[call & put] = "Conflicting sweep conditions; excluded"
    return pd.DataFrame({"signal": labels, "reason": reasons}, index=frame.index)

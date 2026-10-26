"""Timestamp continuity checks shared by signal generation and backtesting."""
from __future__ import annotations

from math import isclose, isfinite
from typing import Any

import pandas as pd


def infer_expected_interval_seconds(
    timestamps: pd.Series,
    *,
    expected_interval_seconds: float | None = None,
) -> float | None:
    """Infer the modal positive spacing, or validate an explicitly configured spacing.

    Ties select the shortest spacing. Inference is only a convenience: if data
    omit bars in a perfectly regular pattern, only a source-specific expected
    interval can identify that systematic omission.
    """
    if expected_interval_seconds is not None:
        if (
            isinstance(expected_interval_seconds, bool)
            or not isinstance(expected_interval_seconds, (int, float))
            or not isfinite(float(expected_interval_seconds))
            or float(expected_interval_seconds) <= 0
        ):
            raise ValueError("expected_interval_seconds must be a finite number > 0")
        return float(expected_interval_seconds)

    parsed = pd.to_datetime(timestamps, errors="raise", utc=True)
    if parsed.isna().any():
        raise ValueError("timestamps must not contain missing values")
    if parsed.duplicated().any():
        raise ValueError("timestamps must be unique")
    if not parsed.is_monotonic_increasing:
        raise ValueError("timestamps must be chronologically sorted")
    if len(parsed) < 2:
        return None

    deltas = parsed.diff().dt.total_seconds().dropna()
    positive = deltas[deltas > 0]
    if positive.empty:
        return None
    counts = positive.value_counts()
    highest_count = counts.max()
    candidates = [float(value) for value in counts[counts == highest_count].index]
    return min(candidates)


def continuity_segments(
    candles: pd.DataFrame,
    *,
    expected_interval_seconds: float | None = None,
) -> tuple[pd.Series, float | None, int]:
    """Return a segment label per row, the expected spacing, and break count.

    A row begins a new segment whenever its timestamp is not one expected bar
    after the prior row. With no timestamp column, continuity is unknown and
    all rows are left in one segment for backwards compatibility with small
    indicator examples that contain prices only.
    """
    if "timestamp" not in candles.columns:
        # Still validate explicit parameters so a typo does not go unnoticed.
        if expected_interval_seconds is not None:
            infer_expected_interval_seconds(pd.Series(dtype="datetime64[ns, UTC]"),
                                            expected_interval_seconds=expected_interval_seconds)
        return pd.Series(0, index=candles.index, dtype="int64"), None, 0

    timestamps = pd.to_datetime(candles["timestamp"], errors="raise", utc=True)
    if timestamps.isna().any():
        raise ValueError("timestamps must not contain missing values")
    if timestamps.duplicated().any():
        raise ValueError("timestamps must be unique")
    if not timestamps.is_monotonic_increasing:
        raise ValueError("timestamps must be chronologically sorted")

    interval = infer_expected_interval_seconds(
        timestamps,
        expected_interval_seconds=expected_interval_seconds,
    )
    if interval is None or len(timestamps) < 2:
        return pd.Series(0, index=candles.index, dtype="int64"), interval, 0

    deltas = timestamps.diff().dt.total_seconds()
    contiguous = deltas.map(
        lambda value: False
        if pd.isna(value)
        else isclose(float(value), interval, rel_tol=0.0, abs_tol=1e-6)
    )
    breaks = ~contiguous
    if len(breaks):
        breaks.iloc[0] = False
    segment_ids = breaks.astype("int64").cumsum()
    segment_ids.index = candles.index
    return segment_ids, interval, int(breaks.sum())

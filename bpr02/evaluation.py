"""Chronological and expanding-window splits for leakage-aware research."""
from __future__ import annotations

from typing import TypeAlias

import pandas as pd

Split: TypeAlias = tuple[slice, slice]


def chronological_split(
    frame: pd.DataFrame,
    train_fraction: float = 0.70,
    *,
    timestamp_column: str | None = "timestamp",
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Split rows into an earlier train segment and a later test segment.

    Input order is preserved; rows are never shuffled. If a timestamp column
    is specified, it must exist, be strictly increasing and contain no
    duplicates. This helps prevent silent chronological leakage.
    """
    if not 0.0 < train_fraction < 1.0:
        raise ValueError("train_fraction must be strictly between 0 and 1")
    if len(frame) < 2:
        raise ValueError("at least two rows are needed for a train/test split")
    if timestamp_column is not None:
        if timestamp_column not in frame.columns:
            raise ValueError(f"missing timestamp column: {timestamp_column}")
        timestamps = pd.to_datetime(frame[timestamp_column], errors="raise", utc=True)
        if timestamps.isna().any() or timestamps.duplicated().any():
            raise ValueError("timestamps must be present and unique")
        if not timestamps.is_monotonic_increasing:
            raise ValueError("frame must be chronologically sorted before splitting")

    split_at = int(len(frame) * train_fraction)
    if split_at < 1 or split_at >= len(frame):
        raise ValueError("train_fraction leaves an empty train or test segment")
    return frame.iloc[:split_at].copy(), frame.iloc[split_at:].copy()


def expanding_window_splits(
    n_rows: int,
    *,
    initial_train_size: int,
    test_size: int,
    step_size: int | None = None,
) -> list[Split]:
    """Return expanding train/test slices suitable for chronological evaluation.

    Training starts at row zero and grows by one step for each fold. The next
    test block begins after the training block. By default test blocks do not
    overlap because step_size equals test_size. Incomplete final test blocks
    are omitted rather than silently shortened.
    """
    if n_rows < 2:
        raise ValueError("n_rows must be >= 2")
    if initial_train_size < 1:
        raise ValueError("initial_train_size must be >= 1")
    if test_size < 1:
        raise ValueError("test_size must be >= 1")
    step = test_size if step_size is None else step_size
    if step < 1:
        raise ValueError("step_size must be >= 1")
    if initial_train_size + test_size > n_rows:
        raise ValueError("not enough rows for an initial train and test window")

    splits: list[Split] = []
    test_start = initial_train_size
    while test_start + test_size <= n_rows:
        splits.append((slice(0, test_start), slice(test_start, test_start + test_size)))
        test_start += step
    if not splits:
        raise ValueError("no complete walk-forward split could be built")
    return splits

import pandas as pd
import pytest

from bpr02.evaluation import chronological_split, expanding_window_splits


def test_chronological_split_keeps_past_before_future():
    frame = pd.DataFrame({
        "timestamp": pd.date_range("2025-01-01", periods=10, freq="min", tz="UTC"),
        "close": range(10),
    })
    train, test = chronological_split(frame, train_fraction=0.6)
    assert len(train) == 6
    assert len(test) == 4
    assert train["timestamp"].max() < test["timestamp"].min()
    assert train["close"].tolist() == list(range(6))


def test_chronological_split_rejects_unsorted_or_duplicate_timestamps():
    frame = pd.DataFrame({
        "timestamp": pd.to_datetime(["2025-01-01T00:01:00Z", "2025-01-01T00:00:00Z"]),
        "close": [2, 1],
    })
    with pytest.raises(ValueError, match="chronologically sorted"):
        chronological_split(frame)
    with pytest.raises(ValueError):
        chronological_split(frame, train_fraction=1.0)


def test_expanding_walk_forward_windows_are_ordered_and_disjoint_by_default():
    splits = expanding_window_splits(10, initial_train_size=4, test_size=2)
    assert len(splits) == 3
    assert splits[0] == (slice(0, 4), slice(4, 6))
    assert splits[1] == (slice(0, 6), slice(6, 8))
    assert splits[2] == (slice(0, 8), slice(8, 10))


def test_expanding_walk_forward_rejects_insufficient_data():
    with pytest.raises(ValueError):
        expanding_window_splits(4, initial_train_size=3, test_size=2)

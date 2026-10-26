import pandas as pd
import pytest

from bpr02.walkforward import run_walk_forward


def sample_candles(n=10):
    close = [10, 11, 10, 9, 10, 12, 10, 8, 9, 10][:n]
    return pd.DataFrame({
        "timestamp": pd.date_range("2025-01-01", periods=n, freq="min", tz="UTC"),
        "open": close,
        "high": [value + 1 for value in close],
        "low": [value - 1 for value in close],
        "close": close,
    })


def test_walk_forward_emits_metrics_for_each_future_test_window():
    trades, metrics = run_walk_forward(
        sample_candles(),
        initial_train_size=4,
        test_size=2,
        lookback=2,
        expiry_bars=1,
    )
    assert metrics["fold"].tolist() == [1, 2, 3]
    assert metrics["training_rows"].tolist() == [4, 6, 8]
    assert metrics["test_rows"].tolist() == [2, 2, 2]
    assert (metrics["training_end"] < metrics["test_start"]).all()
    assert {
        "candidate_signals",
        "eligible_candidate_signals",
        "excluded_due_to_timestamp_gaps",
        "excluded_due_to_missing_future_bars",
        "candidate_coverage_ratio",
    }.issubset(metrics.columns)
    assert (
        metrics["eligible_candidate_signals"]
        + metrics["excluded_due_to_timestamp_gaps"]
        + metrics["excluded_due_to_missing_future_bars"]
        == metrics["candidate_signals"]
    ).all()
    if not trades.empty:
        bounds = metrics.set_index("fold")
        for _, trade in trades.iterrows():
            row = bounds.loc[trade["fold"]]
            assert trade["signal_timestamp"] >= row["test_start"]
            assert trade["expiry_timestamp"] <= row["test_end"]


def test_walk_forward_rejects_unordered_candles():
    frame = sample_candles()
    frame.loc[0, "timestamp"], frame.loc[1, "timestamp"] = (
        frame.loc[1, "timestamp"], frame.loc[0, "timestamp"]
    )
    with pytest.raises(ValueError, match="chronologically sorted"):
        run_walk_forward(frame, initial_train_size=4, test_size=2, lookback=2)

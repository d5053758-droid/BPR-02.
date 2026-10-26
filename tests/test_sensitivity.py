import pandas as pd
import pytest

from bpr02.sensitivity import run_sensitivity_analysis


def sample_candles(n=30):
    close = [10, 11, 10, 9, 10, 12, 10, 8, 9, 10] * (n // 10) + [10, 11, 9, 12, 8, 10][: n % 10]
    close = close[:n]
    return pd.DataFrame({
        "timestamp": pd.date_range("2025-01-01", periods=n, freq="min", tz="UTC"),
        "open": close,
        "high": [value + 1 for value in close],
        "low": [value - 1 for value in close],
        "close": close,
    })


def test_sensitivity_returns_each_grid_combination_for_train_and_test():
    result = run_sensitivity_analysis(
        sample_candles(),
        lookbacks=[2, 4],
        expiry_bars=[1, 2],
        train_fraction=0.6,
    )
    assert len(result) == 2 * 2 * 2
    assert set(result["segment"]) == {"train", "test"}
    assert set(result["lookback"]) == {2, 4}
    assert set(result["expiry_bars"]) == {1, 2}
    assert (result["trades"] >= 0).all()
    assert set(result["candidate_signals"].unique()).issubset(set(range(30)))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"lookbacks": []},
        {"expiry_bars": []},
        {"lookbacks": [1]},
        {"expiry_bars": [0]},
        {"lookbacks": [2, 2]},
        {"expiry_bars": [1, 1]},
    ],
)
def test_sensitivity_rejects_invalid_grids(kwargs):
    with pytest.raises(ValueError):
        run_sensitivity_analysis(sample_candles(), **kwargs)


def test_sensitivity_rejects_unsorted_input():
    frame = sample_candles()
    frame.loc[0, "timestamp"], frame.loc[1, "timestamp"] = (
        frame.loc[1, "timestamp"], frame.loc[0, "timestamp"]
    )
    with pytest.raises(ValueError, match="chronologically sorted"):
        run_sensitivity_analysis(frame, lookbacks=[2], expiry_bars=[1])

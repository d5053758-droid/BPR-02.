import pandas as pd
import pytest

from bpr02.indicators import atr, ema


def test_ema_requires_valid_period_and_uses_minimum_history():
    values = pd.Series([1.0, 2.0, 3.0, 4.0])
    result = ema(values, 3)
    assert result.iloc[:2].isna().all()
    assert result.iloc[2] == pytest.approx(2.25)
    with pytest.raises(ValueError):
        ema(values, 0)


def test_atr_simple_rolling_true_range():
    frame = pd.DataFrame({
        "high": [10.0, 12.0, 13.0],
        "low": [8.0, 9.0, 11.0],
        "close": [9.0, 11.0, 12.0],
    })
    result = atr(frame, period=2)
    assert pd.isna(result.iloc[0])
    assert result.iloc[1] == pytest.approx(2.5)
    with pytest.raises(ValueError):
        atr(frame, period=0)

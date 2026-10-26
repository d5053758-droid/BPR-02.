import pandas as pd
import pytest

from bpr02.backtest import backtest_fixed_expiry, summarize_results
from bpr02.signals import CALL, NO_TRADE, PUT


def candles(closes, opens=None):
    opens = opens or closes
    return pd.DataFrame({
        "timestamp": pd.date_range("2025-01-01", periods=len(closes), freq="min", tz="UTC"),
        "open": opens,
        "close": closes,
    })


def test_signal_enters_next_open_and_expires_on_entry_bar_close():
    data = candles([10, 12, 10], [10, 11, 10])
    signals = pd.Series([CALL, NO_TRADE, NO_TRADE])
    results = backtest_fixed_expiry(data, signals, payout=0.8)
    assert len(results) == 1
    assert results.iloc[0]["entry_price"] == 11
    assert results.iloc[0]["expiry_price"] == 12
    assert results.iloc[0]["outcome"] == "WIN"
    assert results.iloc[0]["pnl"] == pytest.approx(0.8)


def test_put_loss_and_tie_push():
    data = candles([10, 12, 12, 12], [10, 11, 12, 12])
    signals = pd.Series([PUT, CALL, NO_TRADE, NO_TRADE])
    results = backtest_fixed_expiry(data, signals)
    assert results["outcome"].tolist() == ["LOSS", "TIE"]
    assert results["pnl"].tolist() == [-1.0, 0.0]
    stats = summarize_results(results)
    assert stats["trades"] == 2
    assert stats["wins"] == 0
    assert stats["losses"] == 1
    assert stats["ties"] == 1
    assert stats["net_pnl_in_stake_units"] == -1.0


def test_parameters_and_mismatched_lengths_rejected():
    data = candles([1, 2])
    with pytest.raises(ValueError):
        backtest_fixed_expiry(data, pd.Series([CALL, NO_TRADE]), expiry_bars=0)
    with pytest.raises(ValueError):
        backtest_fixed_expiry(data, pd.Series([CALL]), expiry_bars=1)

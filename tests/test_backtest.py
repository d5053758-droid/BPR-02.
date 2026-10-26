import pandas as pd
import pytest

from bpr02.backtest import backtest_fixed_expiry, summarize_results
from bpr02.signals import CALL, NO_TRADE, PUT


def candles(closes, opens=None):
    opens = opens if opens is not None else closes
    highs = [max(open_price, close_price) + 1.0 for open_price, close_price in zip(opens, closes)]
    lows = [min(open_price, close_price) - 1.0 for open_price, close_price in zip(opens, closes)]
    return pd.DataFrame({
        "timestamp": pd.date_range("2025-01-01", periods=len(closes), freq="min", tz="UTC"),
        "open": opens,
        "high": highs,
        "low": lows,
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
    assert stats["break_even_win_rate_excluding_ties"] == pytest.approx(1 / 1.8)
    assert stats["profit_factor"] == 0.0
    assert stats["max_drawdown_in_stake_units"] == -1.0


def test_tie_policy_loss_counts_tie_as_a_loss():
    data = candles([10, 11], [10, 11])
    result = backtest_fixed_expiry(data, pd.Series([CALL, NO_TRADE]), tie_policy="loss")
    assert result.iloc[0]["outcome"] == "LOSS"
    assert result.iloc[0]["pnl"] == -1.0


def test_parameters_and_mismatched_lengths_rejected():
    data = candles([1, 2])
    with pytest.raises(ValueError):
        backtest_fixed_expiry(data, pd.Series([CALL, NO_TRADE]), expiry_bars=0)
    with pytest.raises(ValueError):
        backtest_fixed_expiry(data, pd.Series([CALL]), expiry_bars=1)

def test_summary_reports_profit_factor_and_equity_drawdown():
    results = pd.DataFrame({
        "outcome": ["WIN", "LOSS", "WIN", "LOSS"],
        "pnl": [0.8, -1.0, 0.8, -1.0],
        "payout": [0.8, 0.8, 0.8, 0.8],
    })
    stats = summarize_results(results)
    assert stats["profit_factor"] == pytest.approx(0.8)
    assert stats["max_drawdown_in_stake_units"] == pytest.approx(-1.2)
    assert stats["expectancy_per_trade"] == pytest.approx(-0.1)



def test_backtest_rejects_duplicate_or_unordered_timestamps():
    data = candles([10, 11, 12])
    signals = pd.Series([CALL, NO_TRADE, NO_TRADE])
    duplicate = data.copy()
    duplicate.loc[2, "timestamp"] = duplicate.loc[1, "timestamp"]
    with pytest.raises(ValueError, match="unique"):
        backtest_fixed_expiry(duplicate, signals)

    unordered = data.copy()
    unordered.loc[0, "timestamp"], unordered.loc[1, "timestamp"] = (
        unordered.loc[1, "timestamp"], unordered.loc[0, "timestamp"]
    )
    with pytest.raises(ValueError, match="chronologically sorted"):
        backtest_fixed_expiry(unordered, signals)


@pytest.mark.parametrize(
    ("column", "value", "message"),
    [
        ("open", float("inf"), "finite"),
        ("high", float("nan"), "finite"),
        ("low", "not-a-price", "numeric"),
    ],
)
def test_backtest_rejects_nonfinite_or_nonnumeric_prices(column, value, message):
    data = candles([10, 11, 12])
    data[column] = data[column].astype(object)
    data.loc[1, column] = value
    with pytest.raises(ValueError, match=message):
        backtest_fixed_expiry(data, pd.Series([CALL, NO_TRADE, NO_TRADE]))


def test_backtest_rejects_invalid_ohlc_ranges_even_for_direct_callers():
    data = candles([10, 11, 12])
    data.loc[1, "high"] = 9.0
    with pytest.raises(ValueError, match="invalid OHLC"):
        backtest_fixed_expiry(data, pd.Series([CALL, NO_TRADE, NO_TRADE]))


@pytest.mark.parametrize("payout", [float("nan"), float("inf"), -0.01, 1.01, True])
def test_backtest_rejects_invalid_payout_values(payout):
    with pytest.raises(ValueError, match="payout"):
        backtest_fixed_expiry(candles([10, 11]), pd.Series([CALL, NO_TRADE]), payout=payout)


@pytest.mark.parametrize("stake", [float("nan"), float("inf"), 0.0, -1.0, True])
def test_backtest_rejects_invalid_stake_values(stake):
    with pytest.raises(ValueError, match="stake"):
        backtest_fixed_expiry(candles([10, 11]), pd.Series([CALL, NO_TRADE]), stake=stake)


@pytest.mark.parametrize("expiry_bars", [0, -1, 1.5, True])
def test_backtest_rejects_noninteger_or_invalid_expiry_values(expiry_bars):
    with pytest.raises(ValueError, match="expiry_bars"):
        backtest_fixed_expiry(candles([10, 11]), pd.Series([CALL, NO_TRADE]), expiry_bars=expiry_bars)


def test_backtest_rejects_unknown_signal_labels_instead_of_silently_ignoring():
    with pytest.raises(ValueError, match="unsupported label"):
        backtest_fixed_expiry(candles([10, 11]), pd.Series(["BUY", NO_TRADE]))


def test_backtest_accepts_case_insensitive_valid_signal_labels():
    results = backtest_fixed_expiry(
        candles([10, 12], [10, 11]),
        pd.Series(["call", "no trade"]),
        payout=0.80,
    )
    assert len(results) == 1
    assert results.iloc[0]["direction"] == CALL
    assert results.iloc[0]["outcome"] == "WIN"


def test_backtest_rejects_missing_timestamps():
    data = candles([10, 11])
    data.loc[0, "timestamp"] = pd.NaT
    with pytest.raises(ValueError, match="must not be missing"):
        backtest_fixed_expiry(data, pd.Series([CALL, NO_TRADE]))


def test_bar_backtest_omits_trades_that_cross_missing_candle_intervals():
    data = candles([10, 11, 12], [10, 10, 11])
    data["timestamp"] = pd.to_datetime([
        "2025-01-01T00:00:00Z",
        "2025-01-01T00:01:00Z",
        "2025-01-01T00:03:00Z",
    ])
    signals = pd.Series([CALL, CALL, NO_TRADE])

    one_bar = backtest_fixed_expiry(data, signals, expiry_bars=1)
    assert one_bar["signal_timestamp"].astype(str).tolist() == [
        "2025-01-01 00:00:00+00:00"
    ]

    two_bars = backtest_fixed_expiry(data, signals, expiry_bars=2)
    assert two_bars.empty


def test_backtest_coverage_diagnostics_counts_timestamp_and_future_exclusions():
    from bpr02.backtest import backtest_coverage_diagnostics

    data = candles([10, 11, 12], [10, 10, 11])
    data["timestamp"] = pd.to_datetime([
        "2025-01-01T00:00:00Z",
        "2025-01-01T00:01:00Z",
        "2025-01-01T00:03:00Z",
    ])
    signals = pd.Series([CALL, CALL, NO_TRADE])

    diagnostics = backtest_coverage_diagnostics(data, signals, expiry_bars=1)
    assert diagnostics["candidate_signals"] == 2
    assert diagnostics["eligible_candidate_signals"] == 1
    assert diagnostics["excluded_due_to_timestamp_gaps"] == 1
    assert diagnostics["excluded_due_to_missing_future_bars"] == 0
    assert diagnostics["timestamp_discontinuities"] == 1
    assert diagnostics["candidate_coverage_ratio"] == pytest.approx(0.5)

    longer_expiry = backtest_coverage_diagnostics(data, signals, expiry_bars=2)
    assert longer_expiry["eligible_candidate_signals"] == 0
    assert longer_expiry["excluded_due_to_timestamp_gaps"] == 1
    assert longer_expiry["excluded_due_to_missing_future_bars"] == 1

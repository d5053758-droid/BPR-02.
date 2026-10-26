import json

import pandas as pd
import pytest

from bpr02.subminute import backtest_seconds_expiry, load_quote_csv, main


def signal_candles(timestamps=None):
    if timestamps is None:
        timestamps = pd.to_datetime(
            ["2025-01-01T00:00:00Z", "2025-01-01T00:01:00Z"],
            utc=True,
        )
    return pd.DataFrame({
        "timestamp": timestamps,
        "open": [1.1000] * len(timestamps),
        "high": [1.1010] * len(timestamps),
        "low": [1.0990] * len(timestamps),
        "close": [1.1005] * len(timestamps),
    })


def quotes(rows):
    return pd.DataFrame({
        "timestamp": pd.to_datetime([row[0] for row in rows], utc=True),
        "price": [row[1] for row in rows],
    })


def test_seconds_backtest_enters_after_bar_close_and_settles_three_seconds_after_entry():
    candles = signal_candles()
    labels = pd.Series(["CALL", "NO TRADE"])
    quote_data = quotes([
        ("2025-01-01T00:01:00.100Z", 1.1000),
        ("2025-01-01T00:01:01.000Z", 1.1005),
        ("2025-01-01T00:01:03.100Z", 1.1010),
    ])

    trades, diagnostics = backtest_seconds_expiry(
        candles, labels, quote_data, expiry_seconds=3, max_quote_delay_seconds=0.25
    )

    assert len(trades) == 1
    trade = trades.iloc[0]
    assert trade["signal_available_at"] == pd.Timestamp("2025-01-01T00:01:00Z")
    assert trade["entry_timestamp"] == pd.Timestamp("2025-01-01T00:01:00.100Z")
    assert trade["expiry_target_timestamp"] == pd.Timestamp("2025-01-01T00:01:03.100Z")
    assert trade["settlement_timestamp"] == trade["expiry_target_timestamp"]
    assert trade["direction"] == "CALL"
    assert trade["outcome"] == "WIN"
    assert trade["pnl"] == pytest.approx(0.8)
    assert diagnostics["expiry_seconds"] == 3.0
    assert diagnostics["modeled_trades"] == 1
    assert sum(diagnostics[key] for key in (
        "no_entry_quote", "stale_entry_quote", "no_settlement_quote", "stale_settlement_quote"
    )) == 0


def test_seconds_backtest_reports_missing_entry_quotes_instead_of_fabricating_trades():
    candles = signal_candles()
    labels = pd.Series(["CALL", "PUT"])
    quote_data = quotes([
        ("2025-01-01T00:01:00.100Z", 1.1000),
        ("2025-01-01T00:01:03.100Z", 1.1010),
    ])

    trades, diagnostics = backtest_seconds_expiry(
        candles, labels, quote_data, expiry_seconds=3, max_quote_delay_seconds=0.25
    )

    assert len(trades) == 1
    assert diagnostics["candidate_signals"] == 2
    assert diagnostics["no_entry_quote"] == 1
    assert diagnostics["modeled_trades"] == 1


def test_seconds_backtest_rejects_stale_entry_or_settlement_quotes():
    candles = signal_candles()
    labels = pd.Series(["CALL", "NO TRADE"])
    stale_entry = quotes([("2025-01-01T00:01:02Z", 1.1)])
    trades, diagnostics = backtest_seconds_expiry(
        candles, labels, stale_entry, expiry_seconds=3, max_quote_delay_seconds=0.5
    )
    assert trades.empty
    assert diagnostics["stale_entry_quote"] == 1

    missing_settlement = quotes([("2025-01-01T00:01:00.100Z", 1.1)])
    trades, diagnostics = backtest_seconds_expiry(
        candles, labels, missing_settlement, expiry_seconds=3, max_quote_delay_seconds=0.5
    )
    assert trades.empty
    assert diagnostics["no_settlement_quote"] == 1


def test_seconds_backtest_applies_tie_policy_and_entry_delay():
    candles = signal_candles()
    labels = pd.Series(["CALL", "NO TRADE"])
    quote_data = quotes([
        ("2025-01-01T00:01:00.200Z", 1.1),
        ("2025-01-01T00:01:03.200Z", 1.1),
    ])
    trades, diagnostics = backtest_seconds_expiry(
        candles, labels, quote_data,
        expiry_seconds=3, entry_delay_seconds=0.1,
        max_quote_delay_seconds=0.2, tie_policy="loss",
    )
    assert len(trades) == 1
    assert trades.iloc[0]["entry_quote_delay_seconds"] == pytest.approx(0.1)
    assert trades.iloc[0]["outcome"] == "LOSS"
    assert trades.iloc[0]["pnl"] == -1.0
    assert diagnostics["entry_delay_seconds"] == 0.1


@pytest.mark.parametrize(
    "values",
    [
        ["2025-01-01T00:00:00", "2025-01-01T00:00:01Z"],
        ["not-a-timestamp", "2025-01-01T00:00:01Z"],
    ],
)
def test_seconds_backtest_rejects_timezone_naive_or_invalid_signal_timestamps(values):
    candles = signal_candles(pd.Series(values))
    with pytest.raises(ValueError, match="timezone|invalid timestamp"):
        backtest_seconds_expiry(
            candles, pd.Series(["CALL", "NO TRADE"]),
            quotes([("2025-01-01T00:01:00Z", 1.1)]),
        )


def test_seconds_backtest_rejects_duplicate_or_unsorted_quote_timestamps():
    candles = signal_candles()
    labels = pd.Series(["CALL", "NO TRADE"])
    duplicated = quotes([
        ("2025-01-01T00:01:00Z", 1.1),
        ("2025-01-01T00:01:00Z", 1.101),
    ])
    with pytest.raises(ValueError, match="unique"):
        backtest_seconds_expiry(candles, labels, duplicated)

    unsorted = quotes([
        ("2025-01-01T00:01:01Z", 1.1),
        ("2025-01-01T00:01:00Z", 1.101),
    ])
    with pytest.raises(ValueError, match="chronologically sorted"):
        backtest_seconds_expiry(candles, labels, unsorted)


def test_seconds_backtest_rejects_invalid_prices_parameters_and_labels():
    candles = signal_candles()
    labels = pd.Series(["CALL", "NO TRADE"])
    quote_data = quotes([("2025-01-01T00:01:00Z", 1.1)])

    with pytest.raises(ValueError, match="finite"):
        bad_quotes = quote_data.copy()
        bad_quotes.loc[0, "price"] = float("inf")
        backtest_seconds_expiry(candles, labels, bad_quotes)

    with pytest.raises(ValueError, match="expiry_seconds"):
        backtest_seconds_expiry(candles, labels, quote_data, expiry_seconds=0)
    with pytest.raises(ValueError, match="payout"):
        backtest_seconds_expiry(candles, labels, quote_data, payout=1.5)
    with pytest.raises(ValueError, match="unsupported label"):
        backtest_seconds_expiry(candles, pd.Series(["BUY", "NO TRADE"]), quote_data)


def test_quote_csv_loader_requires_timezone_aware_sorted_unique_data(tmp_path):
    naive = tmp_path / "naive.csv"
    naive.write_text("timestamp,price\n2025-01-01 00:00:00,1.1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="timezone"):
        load_quote_csv(naive)

    good = tmp_path / "quotes.csv"
    good.write_text(
        "timestamp,price\n2025-01-01T00:00:00Z,1.1\n2025-01-01T00:00:01+00:00,1.2\n",
        encoding="utf-8",
    )
    result = load_quote_csv(good)
    assert result["timestamp"].is_monotonic_increasing
    assert result["timestamp"].dt.tz is not None


def test_seconds_backtest_cli_writes_trade_file_and_two_source_manifest(tmp_path, capsys):
    candles_path = tmp_path / "candles.csv"
    quotes_path = tmp_path / "quotes.csv"
    output_path = tmp_path / "seconds-trades.csv"
    manifest_path = tmp_path / "seconds-manifest.json"

    pd.DataFrame({
        "timestamp": pd.date_range("2025-01-01T00:00:00Z", periods=5, freq="min"),
        "open": [10.0, 10.0, 10.0, 8.5, 10.0],
        "high": [11.0, 11.0, 11.0, 9.8, 12.0],
        "low": [9.0, 9.5, 9.2, 8.0, 9.5],
        "close": [10.0, 10.5, 10.0, 8.8, 10.5],
    }).to_csv(candles_path, index=False)
    quotes([
        ("2025-01-01T00:05:00.100Z", 10.0),
        ("2025-01-01T00:05:03.100Z", 10.5),
    ]).to_csv(quotes_path, index=False)

    code = main([
        str(candles_path),
        str(quotes_path),
        "--lookback", "3",
        "--expiry-seconds", "3",
        "--max-quote-delay-seconds", "0.25",
        "--output", str(output_path),
        "--manifest-output", str(manifest_path),
        "--quote-source-label", "synthetic_test_feed",
    ])

    result = json.loads(capsys.readouterr().out)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert code == 0
    assert result["mode"] == "seconds_expiry_research_only"
    assert result["orders_submitted"] == 0
    assert output_path.exists()
    assert manifest["mode"] == "seconds_expiry_research_only"
    assert manifest["additional_sources"]["quotes"]["file_name"] == "quotes.csv"
    assert manifest["parameters"]["expiry_seconds"] == 3.0
    assert manifest["parameters"]["quote_source_label"] == "synthetic_test_feed"

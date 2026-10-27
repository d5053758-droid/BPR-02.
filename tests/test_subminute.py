import json

import pandas as pd
import pytest

from bpr02.subminute import backtest_seconds_expiry, load_quote_csv, main, run_seconds_walk_forward


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
    assert diagnostics["unmodeled_signals"] == 0
    assert diagnostics["modeled_signal_fraction"] == pytest.approx(1.0)
    assert diagnostics["quote_observations"] == 3
    assert diagnostics["quote_span_seconds"] == pytest.approx(3.0)
    assert diagnostics["quote_gap_median_seconds"] == pytest.approx(1.5)
    assert diagnostics["quote_gap_max_seconds"] == pytest.approx(2.1)
    assert diagnostics["quote_temporal_resolution_assessment"] == "low_temporal_resolution_for_requested_expiry"
    assert diagnostics["quote_target_p95_gap_seconds"] == pytest.approx(0.75)
    assert diagnostics["quote_resolution_assessment_is_heuristic"] is True
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
        "--min-modeled-trades", "1",
    ])

    captured = capsys.readouterr()
    result = json.loads(captured.out)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert captured.err == ""
    assert code == 0
    assert result["mode"] == "seconds_expiry_research_only"
    assert result["orders_submitted"] == 0
    assert result["model_coverage_gate_passed"] is True
    assert result["minimum_model_coverage"] == pytest.approx(0.95)
    assert result["quote_resolution_gate_enforced"] is False
    assert result["quote_resolution_gate_passed"] is None
    assert result["all_quality_gates_passed"] is True
    assert output_path.exists()
    assert manifest["mode"] == "seconds_expiry_research_only"
    assert manifest["summary"]["model_coverage_gate_passed"] is True
    assert manifest["additional_sources"]["quotes"]["file_name"] == "quotes.csv"
    assert manifest["parameters"]["expiry_seconds"] == 3.0
    assert manifest["parameters"]["quote_source_label"] == "synthetic_test_feed"



def test_seconds_cli_rejects_timezone_naive_ohlc_before_writing_outputs(tmp_path, capsys):
    candles_path = tmp_path / "naive-candles.csv"
    quotes_path = tmp_path / "quotes.csv"
    output_path = tmp_path / "trades.csv"
    manifest_path = tmp_path / "manifest.json"
    candles_path.write_text(
        "timestamp,open,high,low,close\n"
        "2025-01-01 00:00:00,1.1,1.2,1.0,1.1\n"
        "2025-01-01 00:01:00,1.1,1.2,1.0,1.1\n",
        encoding="utf-8",
    )
    quotes_path.write_text(
        "timestamp,price\n"
        "2025-01-01T00:01:00Z,1.1\n"
        "2025-01-01T00:01:03Z,1.2\n",
        encoding="utf-8",
    )
    code = main([
        str(candles_path), str(quotes_path),
        "--output", str(output_path),
        "--manifest-output", str(manifest_path),
    ])
    captured = capsys.readouterr()
    assert code == 2
    assert "timezone/UTC offset" in captured.err
    assert not output_path.exists()
    assert not manifest_path.exists()


def test_seconds_cli_requires_descriptive_quote_source_label(tmp_path, capsys):
    candles_path = tmp_path / "candles.csv"
    quotes_path = tmp_path / "quotes.csv"
    candles_path.write_text(
        "timestamp,open,high,low,close\n"
        "2025-01-01T00:00:00Z,1.1,1.2,1.0,1.1\n",
        encoding="utf-8",
    )
    quotes_path.write_text(
        "timestamp,price\n2025-01-01T00:01:00Z,1.1\n",
        encoding="utf-8",
    )
    code = main([
        str(candles_path), str(quotes_path), "--quote-source-label", "  ",
        "--output", str(tmp_path / "trades.csv"),
        "--manifest-output", str(tmp_path / "manifest.json"),
    ])
    captured = capsys.readouterr()
    assert code == 2
    assert "--quote-source-label" in captured.err



def test_seconds_walk_forward_uses_nonoverlapping_future_only_blocks():
    candles = pd.DataFrame({
        "timestamp": pd.date_range("2025-01-01T00:00:00Z", periods=8, freq="min"),
        "open": [1.1] * 8,
        "high": [1.101] * 8,
        "low": [1.099] * 8,
        "close": [1.1005] * 8,
    })
    labels = pd.Series([
        "NO TRADE", "NO TRADE", "CALL", "PUT",
        "NO TRADE", "CALL", "PUT", "NO TRADE",
    ])
    quote_times = pd.date_range("2025-01-01T00:00:00Z", periods=10 * 60, freq="s")
    quote_data = pd.DataFrame({
        "timestamp": quote_times,
        "price": [1.1000 + i * 0.000001 for i in range(len(quote_times))],
    })

    trades, folds, diagnostics = run_seconds_walk_forward(
        candles, labels, quote_data,
        initial_train_size=2, test_size=2, step_size=2,
        expiry_seconds=3, max_quote_delay_seconds=0,
    )

    assert folds["fold"].tolist() == [1, 2, 3]
    assert folds["training_rows"].tolist() == [2, 4, 6]
    assert folds["test_rows"].tolist() == [2, 2, 2]
    assert (folds["training_end"] < folds["test_start"]).all()
    assert diagnostics["walk_forward_folds"] == 3
    assert diagnostics["candidate_signals"] == 4
    assert diagnostics["modeled_trades"] == 4
    assert diagnostics["unmodeled_signals"] == 0
    assert trades["fold"].tolist() == [1, 1, 2, 3]
    boundaries = folds.set_index("fold")
    for _, trade in trades.iterrows():
        fold = boundaries.loc[trade["fold"]]
        assert trade["signal_bar_timestamp"] >= fold["test_start"]
        assert trade["signal_bar_timestamp"] <= fold["test_end"]


def test_seconds_walk_forward_rejects_overlapping_test_folds():
    candles = signal_candles()
    labels = pd.Series(["CALL", "NO TRADE"])
    quote_data = quotes([("2025-01-01T00:01:00Z", 1.1)])
    with pytest.raises(ValueError, match="step_size must be >= test_size"):
        run_seconds_walk_forward(
            candles, labels, quote_data,
            initial_train_size=2, test_size=2, step_size=1,
        )


def test_seconds_walk_forward_counts_missing_fold_quotes_without_fabricating_trades():
    candles = pd.DataFrame({
        "timestamp": pd.date_range("2025-01-01T00:00:00Z", periods=4, freq="min"),
        "open": [1.1] * 4,
        "high": [1.101] * 4,
        "low": [1.099] * 4,
        "close": [1.1005] * 4,
    })
    labels = pd.Series(["NO TRADE", "NO TRADE", "CALL", "PUT"])
    # Valid quotes exist, but all occur before the first OOS signal can be available.
    quote_data = quotes([
        ("2025-01-01T00:00:00Z", 1.1),
        ("2025-01-01T00:00:01Z", 1.1001),
    ])
    trades, folds, diagnostics = run_seconds_walk_forward(
        candles, labels, quote_data,
        initial_train_size=2, test_size=2, expiry_seconds=3,
    )
    assert trades.empty
    assert len(folds) == 1
    assert diagnostics["candidate_signals"] == 2
    assert diagnostics["modeled_trades"] == 0
    assert diagnostics["no_entry_quote"] == 2
    assert diagnostics["unmodeled_signals"] == 2


def test_seconds_walk_forward_cli_writes_oos_results_and_fold_manifest(tmp_path, capsys):
    candles_path = tmp_path / "candles.csv"
    quotes_path = tmp_path / "quotes.csv"
    output_path = tmp_path / "oos-trades.csv"
    fold_path = tmp_path / "folds.csv"
    manifest_path = tmp_path / "oos-manifest.json"
    candle_times = pd.date_range("2025-01-01T00:00:00Z", periods=8, freq="min")
    pd.DataFrame({
        "timestamp": candle_times,
        "open": [1.1] * 8,
        "high": [1.101, 1.101, 1.101, 1.100, 1.101, 1.101, 1.102, 1.101],
        "low": [1.099, 1.0995, 1.0992, 1.098, 1.0985, 1.099, 1.098, 1.099],
        "close": [1.100, 1.1002, 1.0997, 1.0998, 1.1001, 1.1002, 1.1001, 1.1002],
    }).to_csv(candles_path, index=False)
    quote_times = pd.date_range("2025-01-01T00:00:00Z", periods=10 * 60, freq="s")
    pd.DataFrame({
        "timestamp": quote_times,
        "price": [1.1000 + i * 0.000001 for i in range(len(quote_times))],
    }).to_csv(quotes_path, index=False)

    code = main([
        str(candles_path), str(quotes_path),
        "--lookback", "2",
        "--expiry-seconds", "3",
        "--max-quote-delay-seconds", "0",
        "--walk-forward",
        "--initial-train-size", "2",
        "--test-size", "2",
        "--step-size", "2",
        "--output", str(output_path),
        "--fold-summary", str(fold_path),
        "--manifest-output", str(manifest_path),
        "--quote-source-label", "synthetic_test_feed",
        "--min-modeled-trades", "1",
    ])

    result = json.loads(capsys.readouterr().out)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    fold_rows = pd.read_csv(fold_path)
    assert code == 0
    assert result["mode"] == "seconds_expiry_walk_forward_research_only"
    assert result["orders_submitted"] == 0
    assert result["folds"] == 3
    assert result["candidate_signals"] >= 1
    assert output_path.exists()
    assert len(fold_rows) == 3
    assert manifest["parameters"]["walk_forward"] is True
    assert manifest["outputs"]["fold_summary_csv"] == str(fold_path)
    assert manifest["summary"]["walk_forward_folds"] == 3


def test_seconds_walk_forward_requires_nonoverlapping_output_paths(tmp_path, capsys):
    candles_path = tmp_path / "candles.csv"
    quotes_path = tmp_path / "quotes.csv"
    candles_path.write_text(
        "timestamp,open,high,low,close\n"
        "2025-01-01T00:00:00Z,1.1,1.2,1.0,1.1\n",
        encoding="utf-8",
    )
    quotes_path.write_text(
        "timestamp,price\n2025-01-01T00:01:00Z,1.1\n",
        encoding="utf-8",
    )
    code = main([
        str(candles_path), str(quotes_path), "--walk-forward",
        "--initial-train-size", "2", "--test-size", "2",
        "--output", str(tmp_path / "same.csv"),
        "--fold-summary", str(tmp_path / "same.csv"),
        "--manifest-output", str(tmp_path / "manifest.json"),
    ])
    captured = capsys.readouterr()
    assert code == 2
    assert "output paths must be different" in captured.err



def test_seconds_cli_returns_three_and_saves_outputs_when_model_coverage_is_low(tmp_path, capsys):
    candles_path = tmp_path / "candles.csv"
    quotes_path = tmp_path / "quotes.csv"
    output_path = tmp_path / "trades.csv"
    manifest_path = tmp_path / "manifest.json"

    # Two consecutive sweep/reclaim lows with a short lookback create two candidates.
    pd.DataFrame({
        "timestamp": pd.date_range("2025-01-01T00:00:00Z", periods=5, freq="min"),
        "open": [10.0, 10.0, 9.2, 8.8, 8.8],
        "high": [11.0, 11.5, 10.0, 9.5, 9.2],
        "low": [9.0, 9.5, 8.5, 8.0, 8.2],
        "close": [10.0, 10.0, 9.2, 8.8, 8.9],
    }).to_csv(candles_path, index=False)
    quotes([
        ("2025-01-01T00:03:00Z", 8.9),
        ("2025-01-01T00:03:03Z", 9.0),
    ]).to_csv(quotes_path, index=False)

    code = main([
        str(candles_path), str(quotes_path),
        "--lookback", "2",
        "--expiry-seconds", "3",
        "--max-quote-delay-seconds", "0",
        "--min-model-coverage", "0.95",
        "--output", str(output_path),
        "--manifest-output", str(manifest_path),
        "--quote-source-label", "sparse_test_feed",
    ])
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert result["candidate_signals"] == 2
    assert result["modeled_trades"] == 1
    assert result["unmodeled_signals"] == 1
    assert result["model_coverage_gate_passed"] is False
    assert code == 3
    assert "MODEL COVERAGE THRESHOLD NOT MET" in captured.err
    assert output_path.exists()
    assert manifest["summary"]["model_coverage_gate_passed"] is False


@pytest.mark.parametrize("minimum", [-0.01, 1.01, float("nan"), float("inf")])
def test_seconds_cli_rejects_invalid_model_coverage_threshold(tmp_path, capsys, minimum):
    candles_path = tmp_path / "candles.csv"
    quotes_path = tmp_path / "quotes.csv"
    candles_path.write_text(
        "timestamp,open,high,low,close\n"
        "2025-01-01T00:00:00Z,1.1,1.2,1.0,1.1\n"
        "2025-01-01T00:01:00Z,1.1,1.2,1.0,1.1\n",
        encoding="utf-8",
    )
    quotes_path.write_text(
        "timestamp,price\n2025-01-01T00:01:00Z,1.1\n",
        encoding="utf-8",
    )
    code = main([
        str(candles_path), str(quotes_path),
        "--min-model-coverage", str(minimum),
        "--output", str(tmp_path / "trades.csv"),
        "--manifest-output", str(tmp_path / "manifest.json"),
    ])
    captured = capsys.readouterr()
    assert code == 2
    assert "--min-model-coverage" in captured.err


def test_seconds_cli_resolution_gate_saves_results_but_returns_three(tmp_path, capsys):
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
        str(candles_path), str(quotes_path),
        "--lookback", "3",
        "--expiry-seconds", "3",
        "--max-quote-delay-seconds", "0.25",
        "--min-model-coverage", "0.95",
        "--max-quote-p95-gap-seconds", "0.5",
        "--min-modeled-trades", "1",
        "--output", str(output_path),
        "--manifest-output", str(manifest_path),
        "--quote-source-label", "synthetic_test_feed",
    ])
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert result["candidate_signals"] == 1
    assert result["modeled_trades"] == 1
    assert result["model_coverage_gate_passed"] is True
    assert result["quote_temporal_resolution_assessment"] == "low_temporal_resolution_for_requested_expiry"
    assert result["quote_resolution_gate_enforced"] is True
    assert result["quote_resolution_gate_passed"] is False
    assert result["all_quality_gates_passed"] is False
    assert code == 3
    assert "QUOTE RESOLUTION THRESHOLD NOT MET" in captured.err
    assert output_path.exists()
    assert manifest["summary"]["quote_resolution_gate_passed"] is False
    assert manifest["summary"]["max_quote_p95_gap_seconds"] == pytest.approx(0.5)


@pytest.mark.parametrize("threshold", [-0.1, float("nan"), float("inf")])
def test_seconds_cli_rejects_invalid_quote_resolution_gate(tmp_path, capsys, threshold):
    candles_path = tmp_path / "candles.csv"
    quotes_path = tmp_path / "quotes.csv"
    candles_path.write_text(
        "timestamp,open,high,low,close\\n"
        "2025-01-01T00:00:00Z,1.1,1.2,1.0,1.1\\n"
        "2025-01-01T00:01:00Z,1.1,1.2,1.0,1.1\\n",
        encoding="utf-8",
    )
    quotes_path.write_text(
        "timestamp,price\\n2025-01-01T00:01:00Z,1.1\\n"
        "2025-01-01T00:01:03Z,1.2\\n",
        encoding="utf-8",
    )
    code = main([
        str(candles_path), str(quotes_path),
        "--max-quote-p95-gap-seconds", str(threshold),
        "--output", str(tmp_path / "trades.csv"),
        "--manifest-output", str(tmp_path / "manifest.json"),
    ])
    captured = capsys.readouterr()
    assert code == 2
    assert "--max-quote-p95-gap-seconds" in captured.err



def test_walk_forward_resolution_gate_uses_test_fold_quotes_not_dense_quotes_elsewhere(tmp_path, capsys, monkeypatch):
    candles_path = tmp_path / "candles.csv"
    quotes_path = tmp_path / "quotes.csv"
    output_path = tmp_path / "oos-trades.csv"
    folds_path = tmp_path / "folds.csv"
    manifest_path = tmp_path / "manifest.json"

    candle_times = pd.date_range("2025-01-01T00:00:00Z", periods=6, freq="min")
    pd.DataFrame({
        "timestamp": candle_times,
        "open": [1.1] * 6,
        "high": [1.101] * 6,
        "low": [1.099] * 6,
        "close": [1.1005] * 6,
    }).to_csv(candles_path, index=False)

    # The full file is dense before and after the test window, but the relevant
    # out-of-sample fold has only entry/settlement quotes separated by 3 seconds.
    dense_before = pd.date_range(
        "2025-01-01T00:00:00Z", "2025-01-01T00:01:59.500Z", freq="500ms"
    )
    sparse_test_quotes = pd.to_datetime(
        ["2025-01-01T00:03:00Z", "2025-01-01T00:03:03Z"], utc=True
    )
    dense_after = pd.date_range(
        "2025-01-01T00:08:00Z", "2025-01-01T00:10:00Z", freq="500ms"
    )
    quote_times = dense_before.append(sparse_test_quotes).append(dense_after)
    pd.DataFrame({
        "timestamp": quote_times,
        "price": [1.1 + index * 0.000001 for index in range(len(quote_times))],
    }).to_csv(quotes_path, index=False)

    # Pin the signal stream to one causal candidate inside the first test fold.
    import bpr02.subminute as subminute_module
    monkeypatch.setattr(
        subminute_module,
        "sweep_reclaim_signals",
        lambda frame, lookback: pd.DataFrame({
            "signal": ["NO TRADE", "NO TRADE", "CALL", "NO TRADE", "NO TRADE", "NO TRADE"]
        }),
    )

    code = main([
        str(candles_path), str(quotes_path),
        "--bar-seconds", "60",
        "--expiry-seconds", "3",
        "--max-quote-delay-seconds", "0.25",
        "--walk-forward",
        "--initial-train-size", "2",
        "--test-size", "2",
        "--step-size", "2",
        "--min-model-coverage", "1.0",
        "--min-modeled-trades", "1",
        "--max-quote-p95-gap-seconds", "0.75",
        "--output", str(output_path),
        "--fold-summary", str(folds_path),
        "--manifest-output", str(manifest_path),
        "--quote-source-label", "synthetic_sparse_oos_test",
    ])
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert result["candidate_signals"] == 1
    assert result["modeled_trades"] == 1
    assert result["quote_gap_p95_seconds"] < 0.75
    assert result["out_of_sample_quote_gap_p95_seconds"] > 0.75
    assert result["out_of_sample_quote_p95_measurement_complete"] is True
    assert result["quote_resolution_gate_scope"] == "out_of_sample_test_folds"
    assert result["quote_resolution_gate_p95_gap_seconds"] > 0.75
    assert result["quote_resolution_gate_passed"] is False
    assert result["all_quality_gates_passed"] is False
    assert code == 3
    assert "QUOTE RESOLUTION THRESHOLD NOT MET" in captured.err
    assert manifest["summary"]["quote_resolution_gate_scope"] == "out_of_sample_test_folds"
    assert manifest["summary"]["quote_resolution_gate_passed"] is False


def test_seconds_cli_minimum_modeled_trades_gate_saves_results_but_returns_three(tmp_path, capsys):
    candles_path = tmp_path / "candles.csv"
    quotes_path = tmp_path / "quotes.csv"
    output_path = tmp_path / "trades.csv"
    manifest_path = tmp_path / "manifest.json"

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
        str(candles_path), str(quotes_path),
        "--lookback", "3",
        "--expiry-seconds", "3",
        "--max-quote-delay-seconds", "0.25",
        "--min-modeled-trades", "2",
        "--output", str(output_path),
        "--manifest-output", str(manifest_path),
        "--quote-source-label", "synthetic_test_feed",
    ])
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert code == 3
    assert result["modeled_trades"] == 1
    assert result["model_coverage_gate_passed"] is True
    assert result["modeled_trade_count_gate_passed"] is False
    assert result["minimum_modeled_trades"] == 2
    assert result["all_quality_gates_passed"] is False
    assert "MINIMUM MODELED TRADE COUNT NOT MET" in captured.err
    assert output_path.exists()
    assert manifest["parameters"]["min_modeled_trades"] == 2
    assert manifest["summary"]["modeled_trade_count_gate_passed"] is False
    assert manifest["summary"]["all_quality_gates_passed"] is False


def test_seconds_cli_rejects_minimum_modeled_trades_below_one(tmp_path, capsys):
    candles_path = tmp_path / "candles.csv"
    quotes_path = tmp_path / "quotes.csv"
    candles_path.write_text(
        "timestamp,open,high,low,close\\n"
        "2025-01-01T00:00:00Z,1.1,1.2,1.0,1.1\\n"
        "2025-01-01T00:01:00Z,1.1,1.2,1.0,1.1\\n",
        encoding="utf-8",
    )
    quotes_path.write_text(
        "timestamp,price\\n2025-01-01T00:01:00Z,1.1\\n",
        encoding="utf-8",
    )
    code = main([
        str(candles_path), str(quotes_path),
        "--min-modeled-trades", "0",
        "--output", str(tmp_path / "trades.csv"),
        "--manifest-output", str(tmp_path / "manifest.json"),
    ])
    captured = capsys.readouterr()
    assert code == 2
    assert "--min-modeled-trades" in captured.err

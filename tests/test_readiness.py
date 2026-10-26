import json

import pandas as pd
import pytest

from bpr02.readiness import assess_readiness, main


def write_candles(path, timestamps=None):
    if timestamps is None:
        timestamps = pd.date_range("2025-01-01T00:00:00Z", periods=4, freq="min")
    frame = pd.DataFrame({
        "timestamp": timestamps,
        "open": [1.0, 1.1, 1.2, 1.3][:len(timestamps)],
        "high": [1.1, 1.2, 1.3, 1.4][:len(timestamps)],
        "low": [0.9, 1.0, 1.1, 1.2][:len(timestamps)],
        "close": [1.05, 1.15, 1.25, 1.35][:len(timestamps)],
    })
    frame.to_csv(path, index=False)


def write_quotes(path, start="2025-01-01T00:00:00Z", end="2025-01-01T00:04:05Z", freq="100ms"):
    timestamps = pd.date_range(start, end, freq=freq, tz="UTC") if not pd.Timestamp(start).tzinfo else pd.date_range(start, end, freq=freq)
    frame = pd.DataFrame({
        "timestamp": timestamps,
        "price": [1.1000 + index * 0.000001 for index in range(len(timestamps))],
    })
    frame.to_csv(path, index=False)


def test_candle_only_preflight_ready_requires_explicit_interval(tmp_path, capsys):
    candles = tmp_path / "candles.csv"
    output = tmp_path / "readiness.json"
    write_candles(candles)
    code = main([
        str(candles),
        "--expected-interval-seconds", "60",
        "--output", str(output),
    ])
    capsys.readouterr()
    report = json.loads(output.read_text(encoding="utf-8"))
    assert code == 0
    assert report["readiness_status"] == "ready_for_bar_based_research"
    assert report["overall_ready"] is True
    assert report["gates"]["explicit_candle_interval"]["passed"] is True
    assert report["quote_quality"] is None
    assert report["orders_submitted"] == 0


def test_preflight_blocks_inferred_interval_even_if_candles_look_regular(tmp_path, capsys):
    candles = tmp_path / "candles.csv"
    output = tmp_path / "readiness.json"
    write_candles(candles)
    code = main([str(candles), "--output", str(output)])
    capsys.readouterr()
    report = json.loads(output.read_text(encoding="utf-8"))
    assert code == 3
    assert report["gates"]["explicit_candle_interval"]["passed"] is False
    assert report["readiness_status"] == "not_ready"


def test_preflight_fails_when_missing_bars_exceed_budget(tmp_path, capsys):
    candles = tmp_path / "gapped.csv"
    output = tmp_path / "readiness.json"
    write_candles(candles, pd.to_datetime([
        "2025-01-01T00:00:00Z",
        "2025-01-01T00:01:00Z",
        "2025-01-01T00:03:00Z",
        "2025-01-01T00:04:00Z",
    ]))
    code = main([
        str(candles),
        "--expected-interval-seconds", "60",
        "--max-missing-bars", "0",
        "--output", str(output),
    ])
    capsys.readouterr()
    report = json.loads(output.read_text(encoding="utf-8"))
    assert code == 3
    assert report["candle_quality"]["estimated_missing_bars"] == 1
    assert report["gates"]["candle_gaps_within_budget"]["passed"] is False


def test_seconds_preflight_requires_quote_resolution_and_time_coverage(tmp_path, capsys):
    candles = tmp_path / "candles.csv"
    quotes = tmp_path / "quotes.csv"
    output = tmp_path / "readiness.json"
    write_candles(candles)
    write_quotes(quotes)
    code = main([
        str(candles),
        "--quotes-csv", str(quotes),
        "--expected-interval-seconds", "60",
        "--expiry-seconds", "3",
        "--max-quote-delay-seconds", "1",
        "--output", str(output),
    ])
    capsys.readouterr()
    report = json.loads(output.read_text(encoding="utf-8"))
    assert code == 0
    assert report["readiness_status"] == "ready_for_seconds_expiry_research"
    assert report["gates"]["quote_temporal_resolution"]["passed"] is True
    assert report["gates"]["quote_covers_candle_start"]["passed"] is True
    assert report["gates"]["quote_covers_settlement_horizon"]["passed"] is True
    assert report["quote_coverage"]["settlement_horizon_covered"] is True
    assert report["market_feed_verified"] is False
    assert report["profitability_assessed"] is False


def test_seconds_preflight_fails_closed_when_quote_history_ends_too_early(tmp_path, capsys):
    candles = tmp_path / "candles.csv"
    quotes = tmp_path / "quotes.csv"
    output = tmp_path / "readiness.json"
    write_candles(candles)
    write_quotes(quotes, end="2025-01-01T00:03:00Z")
    code = main([
        str(candles), "--quotes-csv", str(quotes),
        "--expected-interval-seconds", "60",
        "--output", str(output),
    ])
    capsys.readouterr()
    report = json.loads(output.read_text(encoding="utf-8"))
    assert code == 3
    assert report["gates"]["quote_covers_settlement_horizon"]["passed"] is False


def test_preflight_rejects_output_overwriting_input(tmp_path, capsys):
    candles = tmp_path / "candles.csv"
    write_candles(candles)
    before = candles.read_bytes()
    code = main([str(candles), "--expected-interval-seconds", "60", "--output", str(candles)])
    error = capsys.readouterr().err
    assert code == 2
    assert "must not overwrite" in error
    assert candles.read_bytes() == before


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"max_missing_bars": -1}, "max_missing_bars"),
        ({"expiry_seconds": 0}, "expiry_seconds"),
        ({"target_resolution_fraction": 0}, "target_resolution_fraction"),
        ({"target_resolution_fraction": 1.1}, "target_resolution_fraction"),
        ({"max_quote_p95_gap_seconds": -1}, "max_quote_p95_gap_seconds"),
        ({"max_quote_delay_seconds": -1}, "max_quote_delay_seconds"),
        ({"bar_timestamp_label": "middle"}, "bar_timestamp_label"),
    ],
)
def test_assess_readiness_rejects_invalid_parameters(kwargs, message):
    with pytest.raises(ValueError, match=message):
        assess_readiness({}, expected_interval_seconds=None, **kwargs)

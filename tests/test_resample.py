import pandas as pd
import pytest

from bpr02.resample import main, resample_ohlc_to_interval
from bpr02.signals import NO_TRADE, sweep_reclaim_signals


def source_candles(start="2025-01-01T00:00:00Z", periods=24):
    timestamps = pd.date_range(start, periods=periods, freq="5s", tz="UTC")
    opens = [1.1000 + index * 0.0001 for index in range(periods)]
    closes = [value + 0.00002 for value in opens]
    highs = [value + 0.00005 for value in closes]
    lows = [value - 0.00005 for value in opens]
    return pd.DataFrame({
        "timestamp": timestamps,
        "open": opens,
        "high": highs,
        "low": lows,
        "close": closes,
    })


def test_resample_aggregates_only_complete_minutes_and_preserves_ohlc():
    source = source_candles(periods=24)
    minute, report = resample_ohlc_to_interval(source)
    assert len(minute) == 2
    assert minute["timestamp"].tolist() == [
        pd.Timestamp("2025-01-01T00:00:00Z"),
        pd.Timestamp("2025-01-01T00:01:00Z"),
    ]
    assert minute.iloc[0]["open"] == pytest.approx(source.iloc[0]["open"])
    assert minute.iloc[0]["high"] == pytest.approx(source.iloc[:12]["high"].max())
    assert minute.iloc[0]["low"] == pytest.approx(source.iloc[:12]["low"].min())
    assert minute.iloc[0]["close"] == pytest.approx(source.iloc[11]["close"])
    assert report["complete_target_bars_written"] == 2
    assert report["incomplete_target_buckets_excluded"] == 0
    assert report["complete_bar_coverage_ratio_over_time_span"] == 1.0
    assert report["orders_submitted"] == 0


def test_resample_excludes_partial_bucket_without_fabricating_prices():
    source = source_candles(periods=24).drop(index=3).reset_index(drop=True)
    minute, report = resample_ohlc_to_interval(source)
    assert len(minute) == 1
    assert minute.iloc[0]["timestamp"] == pd.Timestamp("2025-01-01T00:01:00Z")
    assert report["incomplete_target_buckets_excluded"] == 1
    assert report["missing_source_slots_inside_incomplete_buckets"] == 1
    assert report["incomplete_examples"][0]["missing_slot_examples"] == [
        "2025-01-01T00:00:15+00:00"
    ]


def test_resample_counts_a_fully_missing_minute_between_observed_buckets():
    first = source_candles(periods=12)
    third = source_candles(start="2025-01-01T00:02:00Z", periods=12)
    minute, report = resample_ohlc_to_interval(pd.concat([first, third], ignore_index=True))
    assert len(minute) == 2
    assert report["observed_target_buckets"] == 2
    assert report["expected_target_buckets_in_time_span"] == 3
    assert report["entirely_missing_target_buckets_in_span"] == 1
    assert report["complete_bar_coverage_ratio_over_time_span"] == pytest.approx(2 / 3)


@pytest.mark.parametrize(
    ("source_interval", "target_interval"),
    [(0, 60), (5, 0), (7, 60), (60, 60), (-5, 60)],
)
def test_resample_rejects_invalid_intervals(source_interval, target_interval):
    with pytest.raises(ValueError):
        resample_ohlc_to_interval(
            source_candles(),
            source_interval_seconds=source_interval,
            target_interval_seconds=target_interval,
        )


def test_resample_rejects_off_grid_duplicate_and_unordered_timestamps():
    source = source_candles(periods=12)
    off_grid = source.copy()
    off_grid.loc[1, "timestamp"] += pd.Timedelta(seconds=1)
    with pytest.raises(ValueError, match="align"):
        resample_ohlc_to_interval(off_grid)

    duplicate = source.copy()
    duplicate.loc[1, "timestamp"] = duplicate.loc[0, "timestamp"]
    with pytest.raises(ValueError, match="unique"):
        resample_ohlc_to_interval(duplicate)

    unordered = source.iloc[::-1].reset_index(drop=True)
    with pytest.raises(ValueError, match="chronologically sorted"):
        resample_ohlc_to_interval(unordered)


def test_cli_writes_report_manifest_and_refuses_overwrite_by_default(tmp_path, capsys):
    source = tmp_path / "source.csv"
    output = tmp_path / "m1.csv"
    report = tmp_path / "report.json"
    manifest = tmp_path / "manifest.json"
    source_candles(periods=24).to_csv(source, index=False)

    code = main([
        str(source), "--output", str(output),
        "--report-output", str(report), "--manifest-output", str(manifest),
    ])
    assert code == 0
    summary = __import__("json").loads(capsys.readouterr().out)
    report_data = __import__("json").loads(report.read_text())
    manifest_data = __import__("json").loads(manifest.read_text())
    assert summary["output_rows"] == 2
    assert len(pd.read_csv(output)) == 2
    assert report_data["source_sha256"]
    assert report_data["output_sha256"]
    assert manifest_data["mode"] == "quality_aware_ohlc_resampling"
    assert manifest_data["source"]["sha256"]
    assert manifest_data["additional_sources"]["resampled_ohlc_output"]["sha256"]
    assert manifest_data["additional_sources"]["quality_report_json"]["sha256"]
    assert manifest_data["dataset"]["rows"] == 2
    assert manifest_data["summary"]["complete_target_bars_written"] == 2

    code = main([
        str(source), "--output", str(output),
        "--report-output", str(report), "--manifest-output", str(manifest),
    ])
    assert code == 2
    assert "output already exists" in capsys.readouterr().err


def test_timestamped_signal_window_resets_after_gap():
    frame = pd.DataFrame({
        "timestamp": pd.to_datetime([
            "2025-01-01T00:00:00Z",
            "2025-01-01T00:01:00Z",
            "2025-01-01T00:02:00Z",
            "2025-01-01T00:03:00Z",
            "2025-01-01T00:06:00Z",
            "2025-01-01T00:07:00Z",
            "2025-01-01T00:08:00Z",
            "2025-01-01T00:09:00Z",
        ]),
        "open": [10.0] * 8,
        "high": [11.0] * 8,
        "low": [9.0] * 8,
        "close": [10.0] * 8,
    })
    signals = sweep_reclaim_signals(frame, lookback=3)
    assert signals["signal"].iloc[4:7].tolist() == [NO_TRADE, NO_TRADE, NO_TRADE]

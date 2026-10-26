import pandas as pd
import pytest

from bpr02.audit import audit_candles


def frame():
    return pd.DataFrame({
        "timestamp": pd.to_datetime([
            "2025-01-01T00:00:00Z",
            "2025-01-01T00:01:00Z",
            "2025-01-01T00:04:00Z",
            "2025-01-01T00:05:00Z",
        ]),
        "open": [1.0, 1.1, 1.2, 1.3],
        "high": [1.1, 1.2, 1.3, 1.4],
        "low": [0.9, 1.0, 1.1, 1.2],
        "close": [1.05, 1.15, 1.25, 1.35],
    })


def test_audit_detects_a_three_minute_gap_as_two_estimated_missing_bars():
    result = audit_candles(frame(), expected_interval_seconds=60)
    assert result["structurally_valid"] is True
    assert result["rows"] == 4
    assert result["gaps_over_1_5x_interval"] == 1
    assert result["estimated_missing_bars"] == 2
    assert result["gap_examples"][0]["gap_seconds"] == 180


def test_audit_flags_invalid_ohlc_duplicate_and_bad_timestamp():
    data = frame()
    data["timestamp"] = data["timestamp"].astype("object")
    data.loc[1, "high"] = 0.5
    data.loc[2, "timestamp"] = data.loc[0, "timestamp"]
    data.loc[3, "timestamp"] = "not-a-timestamp"
    result = audit_candles(data, expected_interval_seconds=60)
    assert result["structurally_valid"] is False
    assert result["timestamp_parse_errors"] == 1
    assert result["duplicate_timestamps"] == 1
    assert result["invalid_ohlc_rows"] >= 1


def test_audit_infers_interval_and_validates_arguments():
    result = audit_candles(frame())
    assert result["interval_source"] == "inferred"
    assert result["expected_interval_seconds"] == 60.0
    with pytest.raises(ValueError):
        audit_candles(frame(), expected_interval_seconds=0)
    with pytest.raises(ValueError):
        audit_candles(frame(), max_gap_examples=-1)

def test_audit_rejects_nonfinite_interval_and_empty_frame_is_not_valid():
    with pytest.raises(ValueError, match="finite value"):
        audit_candles(frame(), expected_interval_seconds=float("nan"))
    empty = pd.DataFrame(columns=["timestamp", "open", "high", "low", "close"])
    result = audit_candles(empty)
    assert result["rows"] == 0
    assert result["structurally_valid"] is False

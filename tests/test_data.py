import pytest

from bpr02.data import DataValidationError, load_ohlc_csv


def test_load_csv_normalizes_columns_and_sorts(tmp_path):
    source = tmp_path / "candles.csv"
    source.write_text(
        "DateTime,Open,High,Low,Close\n"
        "2025-01-01T00:01:00Z,2,3,1,2.5\n"
        "2025-01-01T00:00:00Z,1,2,0.5,1.5\n",
        encoding="utf-8",
    )
    result = load_ohlc_csv(source)
    assert list(result.columns[:5]) == ["timestamp", "open", "high", "low", "close"]
    assert result["timestamp"].is_monotonic_increasing
    assert len(result) == 2


def test_bad_ohlc_range_is_rejected(tmp_path):
    source = tmp_path / "bad.csv"
    source.write_text(
        "timestamp,open,high,low,close\n"
        "2025-01-01T00:00:00Z,5,4,3,4\n",
        encoding="utf-8",
    )
    with pytest.raises(DataValidationError):
        load_ohlc_csv(source)


def test_missing_file_is_reported(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_ohlc_csv(tmp_path / "absent.csv")

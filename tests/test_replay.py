import json

import pandas as pd
import pytest

from bpr02.replay import main


def write_candles(path):
    pd.DataFrame({
        "timestamp": pd.date_range("2025-01-01", periods=5, freq="min", tz="UTC"),
        "open": [1, 2, 3, 4, 5],
        "high": [2, 3, 4, 5, 6],
        "low": [0, 1, 2, 3, 4],
        "close": [1.5, 2.5, 3.5, 4.5, 5.5],
    }).to_csv(path, index=False)


def test_replay_emits_closed_candle_jsonl_with_source_labels(tmp_path, capsys):
    source = tmp_path / "candles.csv"
    write_candles(source)
    code = main([str(source), "--start-index", "1", "--limit", "2", "--symbol", "EURUSD", "--timeframe", "M1"])
    lines = capsys.readouterr().out.splitlines()
    events = [json.loads(line) for line in lines]
    assert code == 0
    assert len(events) == 2
    assert events[0]["closed"] is True
    assert events[0]["symbol"] == "EURUSD"
    assert events[0]["timeframe"] == "M1"
    assert events[0]["source"] == "historical_csv_replay"
    assert events[0]["source_mode"] == "historical_replay_not_live"
    assert events[0]["timestamp"] < events[1]["timestamp"]


@pytest.mark.parametrize(
    "args",
    [
        ["--interval-seconds", "-1"],
        ["--start-index", "-1"],
        ["--limit", "0"],
    ],
)
def test_replay_rejects_invalid_parameters(tmp_path, args, capsys):
    source = tmp_path / "candles.csv"
    write_candles(source)
    code = main([str(source), *args])
    assert code == 2
    assert "ERROR:" in capsys.readouterr().err


def test_replay_rejects_start_index_at_end(tmp_path, capsys):
    source = tmp_path / "candles.csv"
    write_candles(source)
    code = main([str(source), "--start-index", "5"])
    assert code == 2
    assert "at or beyond" in capsys.readouterr().err

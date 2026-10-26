import json

import pandas as pd

from bpr02.cli import main


def write_sample_csv(path):
    close = [10, 11, 10, 9, 10, 12, 10, 8, 9, 10]
    frame = pd.DataFrame({
        "timestamp": pd.date_range("2025-01-01", periods=len(close), freq="min", tz="UTC"),
        "open": close,
        "high": [value + 1 for value in close],
        "low": [value - 1 for value in close],
        "close": close,
    })
    frame.to_csv(path, index=False)


def test_cli_walk_forward_writes_trades_and_fold_summary(tmp_path, capsys):
    source = tmp_path / "candles.csv"
    results = tmp_path / "trades.csv"
    summary = tmp_path / "folds.csv"
    manifest = tmp_path / "run-manifest.json"
    write_sample_csv(source)

    code = main([
        str(source),
        "--walk-forward",
        "--initial-train-size", "4",
        "--test-size", "2",
        "--lookback", "2",
        "--output", str(results),
        "--fold-summary", str(summary),
        "--manifest-output", str(manifest),
    ])
    output = json.loads(capsys.readouterr().out)
    assert code == 0
    assert output["mode"] == "walk_forward_research_only"
    assert output["folds"] == 3
    assert results.exists()
    assert summary.exists()
    assert len(pd.read_csv(summary)) == 3
    assert output["manifest_file"] == str(manifest)
    saved_manifest = json.loads(manifest.read_text(encoding="utf-8"))
    assert saved_manifest["mode"] == "walk_forward_research_only"
    assert saved_manifest["source"]["sha256"] and len(saved_manifest["source"]["sha256"]) == 64
    assert saved_manifest["dataset"]["rows"] == 10
    assert saved_manifest["parameters"]["lookback"] == 2
    assert saved_manifest["summary"]["folds"] == 3


def test_cli_requires_walk_forward_window_sizes(tmp_path, capsys):
    source = tmp_path / "candles.csv"
    write_sample_csv(source)
    code = main([str(source), "--walk-forward"])
    output = capsys.readouterr().out
    assert code == 2
    assert "requires --initial-train-size and --test-size" in output

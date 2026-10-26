import json

import pandas as pd

from bpr02.cli import main


def write_sample_csv(path):
    close = [10, 11, 10, 9, 10, 12, 10, 8, 9, 10] * 3
    frame = pd.DataFrame({
        "timestamp": pd.date_range("2025-01-01", periods=len(close), freq="min", tz="UTC"),
        "open": close,
        "high": [value + 1 for value in close],
        "low": [value - 1 for value in close],
        "close": close,
    })
    frame.to_csv(path, index=False)


def test_sensitivity_cli_exports_train_and_test_grid(tmp_path, capsys):
    source = tmp_path / "candles.csv"
    output = tmp_path / "sensitivity.csv"
    manifest = tmp_path / "sensitivity-manifest.json"
    write_sample_csv(source)
    code = main([
        str(source),
        "--sensitivity",
        "--lookbacks", "2,4",
        "--expiry-grid", "1,2",
        "--train-fraction", "0.6",
        "--sensitivity-output", str(output),
        "--manifest-output", str(manifest),
    ])
    result = json.loads(capsys.readouterr().out)
    assert code == 0
    assert result["mode"] == "sensitivity_research_only"
    assert result["combinations_per_segment"] == 4
    assert output.exists()
    rows = pd.read_csv(output)
    assert len(rows) == 8
    assert set(rows["segment"]) == {"train", "test"}
    saved_manifest = json.loads(manifest.read_text(encoding="utf-8"))
    assert result["manifest_file"] == str(manifest)
    assert saved_manifest["mode"] == "sensitivity_research_only"
    assert saved_manifest["dataset"]["rows"] == 30
    assert saved_manifest["parameters"]["lookbacks"] == [2, 4]
    assert saved_manifest["summary"]["grid_rows"] == 8

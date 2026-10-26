import json

import pandas as pd

from bpr02.cli import main


def test_audit_cli_writes_json_with_gap_report(tmp_path, capsys):
    source = tmp_path / "candles.csv"
    result_path = tmp_path / "audit.json"
    manifest_path = tmp_path / "audit-manifest.json"
    frame = pd.DataFrame({
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
    frame.to_csv(source, index=False)
    code = main([
        str(source), "--audit-data", "--expected-interval-seconds", "60",
        "--audit-output", str(result_path), "--manifest-output", str(manifest_path),
    ])
    output = json.loads(capsys.readouterr().out)
    report = json.loads(result_path.read_text(encoding="utf-8"))
    assert code == 0
    assert output["mode"] == "data_audit"
    assert report["estimated_missing_bars"] == 2
    assert report["structurally_valid"] is True
    assert output["manifest_file"] == str(manifest_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["mode"] == "data_audit"
    assert manifest["source"]["sha256"]



def test_audit_cli_reports_raw_duplicates_and_invalid_candles(tmp_path, capsys):
    source = tmp_path / "bad_candles.csv"
    result_path = tmp_path / "bad_audit.json"
    manifest_path = tmp_path / "bad-audit-manifest.json"
    frame = pd.DataFrame({
        "timestamp": [
            "2025-01-01T00:01:00Z",
            "2025-01-01T00:00:00Z",
            "2025-01-01T00:00:00Z",
        ],
        "open": [1.0, 1.0, 1.0],
        "high": [1.1, 0.5, 1.1],
        "low": [0.9, 0.9, 0.9],
        "close": [1.05, 1.0, 1.05],
    })
    frame.to_csv(source, index=False)
    code = main([str(source), "--audit-data", "--audit-output", str(result_path), "--manifest-output", str(manifest_path)])
    output = json.loads(capsys.readouterr().out)
    report = json.loads(result_path.read_text(encoding="utf-8"))
    assert code == 2
    assert output["mode"] == "data_audit"
    assert report["duplicate_timestamps"] == 1
    assert report["out_of_order_steps"] >= 1
    assert report["invalid_ohlc_rows"] >= 1
    assert report["structurally_valid"] is False
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["summary"]["structurally_valid"] is False

import json

import pandas as pd

from bpr02.cli import main


def test_audit_cli_writes_json_with_gap_report(tmp_path, capsys):
    source = tmp_path / "candles.csv"
    result_path = tmp_path / "audit.json"
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
        "--audit-output", str(result_path),
    ])
    output = json.loads(capsys.readouterr().out)
    report = json.loads(result_path.read_text(encoding="utf-8"))
    assert code == 0
    assert output["mode"] == "data_audit"
    assert report["estimated_missing_bars"] == 2
    assert report["structurally_valid"] is True

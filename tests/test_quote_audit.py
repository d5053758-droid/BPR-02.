import hashlib
import json

import pandas as pd
import pytest

from bpr02.quote_audit import audit_quote_series, main


def quote_frame(start="2025-01-01T00:00:00Z", count=41, freq="100ms"):
    timestamps = pd.date_range(start, periods=count, freq=freq)
    return pd.DataFrame({
        "timestamp": timestamps,
        "price": [1.1000 + index * 0.000001 for index in range(count)],
    })


def test_quote_audit_recognizes_fine_resolution_as_a_screening_result():
    report = audit_quote_series(quote_frame(), expiry_seconds=3, target_resolution_fraction=0.25)
    assert report["observations"] == 41
    assert report["span_seconds"] == pytest.approx(4.0)
    assert report["median_gap_seconds"] == pytest.approx(0.1)
    assert report["p95_gap_seconds"] == pytest.approx(0.1)
    assert report["target_p95_gap_seconds"] == pytest.approx(0.75)
    assert report["temporal_resolution_assessment"] == "meets_configured_resolution_screen"
    assert report["assessment_is_heuristic"] is True
    assert report["profitability_assessed"] is False
    assert report["broker_quote_parity_verified"] is False


def test_quote_audit_flags_low_temporal_resolution_for_three_second_expiry():
    report = audit_quote_series(
        quote_frame(count=20, freq="2s"),
        expiry_seconds=3,
        target_resolution_fraction=0.25,
    )
    assert report["p95_gap_seconds"] == pytest.approx(2.0)
    assert report["fraction_gaps_within_target"] == 0.0
    assert report["temporal_resolution_assessment"] == "low_temporal_resolution_for_requested_expiry"


def test_quote_audit_reports_insufficient_observations_and_duration():
    single = audit_quote_series(quote_frame(count=1), expiry_seconds=3)
    assert single["temporal_resolution_assessment"] == "insufficient_observations"

    short = audit_quote_series(quote_frame(count=10, freq="100ms"), expiry_seconds=3)
    assert short["temporal_resolution_assessment"] == "insufficient_time_coverage"


def test_quote_audit_rejects_naive_duplicate_unsorted_and_invalid_prices():
    naive = quote_frame()
    naive["timestamp"] = naive["timestamp"].astype(str).str.replace("Z", "", regex=False)
    with pytest.raises(ValueError, match="timezone"):
        audit_quote_series(naive)

    duplicate = quote_frame(count=3)
    duplicate.loc[2, "timestamp"] = duplicate.loc[1, "timestamp"]
    with pytest.raises(ValueError, match="unique"):
        audit_quote_series(duplicate)

    unsorted = quote_frame(count=3)
    unsorted.loc[0, "timestamp"], unsorted.loc[1, "timestamp"] = (
        unsorted.loc[1, "timestamp"], unsorted.loc[0, "timestamp"]
    )
    with pytest.raises(ValueError, match="chronologically sorted"):
        audit_quote_series(unsorted)

    invalid = quote_frame(count=3)
    invalid["price"] = [1.1, float("inf"), 1.2]
    with pytest.raises(ValueError, match="numeric and finite"):
        audit_quote_series(invalid)


@pytest.mark.parametrize(
    ("expiry_seconds", "fraction"),
    [(0, 0.25), (float("inf"), 0.25), (3, 0), (3, 1.1), (True, 0.25)],
)
def test_quote_audit_rejects_invalid_screening_parameters(expiry_seconds, fraction):
    with pytest.raises(ValueError):
        audit_quote_series(
            quote_frame(),
            expiry_seconds=expiry_seconds,
            target_resolution_fraction=fraction,
        )


def test_quote_audit_cli_writes_fingerprinted_report(tmp_path, capsys):
    source = tmp_path / "quotes.csv"
    output = tmp_path / "audit.json"
    quote_frame().to_csv(source, index=False)
    code = main([
        str(source),
        "--expiry-seconds", "3",
        "--target-resolution-fraction", "0.25",
        "--output", str(output),
    ])
    printed = json.loads(capsys.readouterr().out)
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert code == 0
    assert printed["source_sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert saved["mode"] == "quote_temporal_quality_audit"
    assert saved["source_file"] == "quotes.csv"
    assert saved["observations"] == 41


def test_quote_audit_cli_threshold_returns_three_without_deleting_report(tmp_path, capsys):
    source = tmp_path / "quotes.csv"
    output = tmp_path / "audit.json"
    quote_frame(count=10, freq="2s").to_csv(source, index=False)
    code = main([
        str(source),
        "--expiry-seconds", "3",
        "--max-p95-gap-seconds", "1.0",
        "--output", str(output),
    ])
    captured = capsys.readouterr()
    assert code == 3
    assert "QUALITY THRESHOLD NOT MET" in captured.err
    assert output.is_file()
    assert json.loads(output.read_text(encoding="utf-8"))["p95_gap_seconds"] == pytest.approx(2.0)


def test_quote_audit_cli_refuses_to_overwrite_source(tmp_path, capsys):
    source = tmp_path / "quotes.csv"
    quote_frame().to_csv(source, index=False)
    original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    code = main([str(source), "--output", str(source)])
    captured = capsys.readouterr()
    assert code == 2
    assert "must not overwrite" in captured.err
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original_hash

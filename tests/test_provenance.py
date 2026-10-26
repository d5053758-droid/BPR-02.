import hashlib
import json

import pytest

from bpr02.provenance import build_run_manifest, sha256_file, write_run_manifest


def test_sha256_file_matches_standard_library_and_validates_chunk_size(tmp_path):
    source = tmp_path / "candles.csv"
    source.write_bytes(b"timestamp,open,high,low,close\n")
    assert sha256_file(source) == hashlib.sha256(source.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="chunk_size"):
        sha256_file(source, chunk_size=0)


def test_manifest_records_input_fingerprint_parameters_and_outputs(tmp_path):
    source = tmp_path / "candles.csv"
    source.write_text(
        "timestamp,open,high,low,close\n2025-01-01T00:00:00Z,1,2,0.5,1.5\n",
        encoding="utf-8",
    )
    output = tmp_path / "trades.csv"
    destination = tmp_path / "manifest.json"
    manifest = build_run_manifest(
        source_path=source,
        mode="research_only",
        parameters={"lookback": 12, "expiry_bars": 1, "grid": (1, 2)},
        outputs={"trade_results_csv": output},
        rows=1,
        start_timestamp="2025-01-01T00:00:00+00:00",
        end_timestamp="2025-01-01T00:00:00+00:00",
        summary={"trades": 3, "profit_factor": None},
        notes=("Research only",),
    )
    written = write_run_manifest(destination, manifest, source_path=source)
    saved = json.loads(written.read_text(encoding="utf-8"))

    assert saved["manifest_schema_version"] == 1
    assert saved["mode"] == "research_only"
    assert saved["source"]["file_name"] == "candles.csv"
    assert saved["source"]["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()
    assert saved["source"]["bytes"] == source.stat().st_size
    assert saved["dataset"]["rows"] == 1
    assert saved["parameters"]["lookback"] == 12
    assert saved["parameters"]["grid"] == [1, 2]
    assert saved["outputs"]["trade_results_csv"] == str(output)
    assert saved["summary"]["profit_factor"] is None


def test_manifest_refuses_to_overwrite_source_or_declared_run_output(tmp_path):
    source = tmp_path / "candles.csv"
    source.write_text("test input", encoding="utf-8")
    output = tmp_path / "trades.csv"
    manifest = build_run_manifest(
        source_path=source,
        mode="research_only",
        parameters={},
        outputs={"trades": str(output)},
    )

    with pytest.raises(ValueError, match="must not overwrite"):
        write_run_manifest(source, manifest, source_path=source)
    with pytest.raises(ValueError, match="must not overwrite"):
        write_run_manifest(output, manifest, source_path=source)


def test_manifest_rejects_empty_mode_and_negative_rows(tmp_path):
    source = tmp_path / "candles.csv"
    source.write_text("input", encoding="utf-8")
    with pytest.raises(ValueError, match="mode"):
        build_run_manifest(source_path=source, mode="", parameters={}, outputs={})
    with pytest.raises(ValueError, match="rows"):
        build_run_manifest(source_path=source, mode="audit", parameters={}, outputs={}, rows=-1)



def test_manifest_fingerprints_and_protects_additional_source_files(tmp_path):
    source = tmp_path / "candles.csv"
    quotes = tmp_path / "quotes.csv"
    source.write_text("candle data", encoding="utf-8")
    quotes.write_text("quote data", encoding="utf-8")
    output = tmp_path / "trades.csv"
    manifest_path = tmp_path / "manifest.json"
    manifest = build_run_manifest(
        source_path=source,
        mode="seconds_expiry_research_only",
        parameters={"expiry_seconds": 3},
        outputs={"trades": output},
        additional_sources={"quotes": quotes},
    )
    saved = json.loads(
        write_run_manifest(manifest_path, manifest, source_path=source).read_text(encoding="utf-8")
    )
    assert saved["additional_sources"]["quotes"]["sha256"] == hashlib.sha256(quotes.read_bytes()).hexdigest()
    assert saved["additional_sources"]["quotes"]["file_name"] == "quotes.csv"

    with pytest.raises(ValueError, match="must not overwrite"):
        write_run_manifest(quotes, manifest, source_path=source)


def test_manifest_rejects_blank_additional_source_label(tmp_path):
    source = tmp_path / "candles.csv"
    source.write_text("input", encoding="utf-8")
    quotes = tmp_path / "quotes.csv"
    quotes.write_text("quotes", encoding="utf-8")
    with pytest.raises(ValueError, match="labels"):
        build_run_manifest(
            source_path=source,
            mode="audit",
            parameters={},
            outputs={},
            additional_sources={" ": quotes},
        )

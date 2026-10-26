import csv
import io
import json
import os
from datetime import datetime, timezone

import pytest

from bpr02.otc_data import (
    convert_tick_log_to_quotes,
    fetch_historical_candles,
    main,
    record_live_ticks,
)


def _sse(*events):
    chunks = []
    for event, payload in events:
        chunks.append(f"event: {event}\ndata: {json.dumps(payload)}\n\n")
    return "".join(chunks).encode("utf-8")


def _tick(symbol="EURUSD_otc", epoch=1788103260, price=1.1):
    return {"symbol": symbol, "time": epoch, "price": price}


def test_record_live_ticks_uses_bearer_header_and_never_writes_the_key(tmp_path):
    output = tmp_path / "raw.jsonl"
    sse = _sse(
        ("connected", {"symbols": ["EURUSD_otc"]}),
        ("tick", _tick(price=1.10001)),
        ("tick", _tick(symbol="GBPUSD_otc", price=1.27001)),
        ("tick", _tick(epoch=1788103261, price=1.10002)),
    )
    sse = sse.replace(b"event: tick", b": keep-alive\n\nevent: tick", 1)
    observed = {}

    def fake_urlopen(request, timeout):
        observed["url"] = request.full_url
        observed["authorization"] = request.get_header("Authorization")
        observed["accept"] = request.get_header("Accept")
        observed["timeout"] = timeout
        return io.BytesIO(sse)

    result = record_live_ticks(
        api_key="secret-test-key",
        symbols=["EURUSD_otc"],
        output_path=output,
        duration_seconds=60,
        urlopen_fn=fake_urlopen,
    )

    raw = output.read_text(encoding="utf-8")
    records = [json.loads(line) for line in raw.splitlines()]
    assert observed["url"].startswith("https://otcharts.com/v1/stream?")
    assert observed["authorization"] == "Bearer secret-test-key"
    assert observed["accept"] == "text/event-stream"
    assert observed["timeout"] == 60
    assert "secret-test-key" not in raw
    assert result["events_recorded"] == 4
    assert result["tick_events_recorded"] == 2
    assert result["ticks_skipped_for_unrequested_symbols"] == 1
    assert result["orders_submitted"] == 0
    assert len(records) == 4
    assert records[1]["event"] == "heartbeat"
    assert records[2]["payload"]["price"] == pytest.approx(1.10001)
    assert records[2]["source_time_epoch_seconds"] == 1788103260


def test_record_live_ticks_requires_key_and_refuses_overwrite_before_network(tmp_path):
    output = tmp_path / "existing.jsonl"
    output.write_text("keep this\n", encoding="utf-8")
    called = False

    def should_not_call(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("network must not be opened")

    with pytest.raises(ValueError, match="OTCHARTS_API_KEY"):
        record_live_ticks(api_key="", output_path=tmp_path / "new.jsonl")
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        record_live_ticks(
            api_key="test-key",
            output_path=output,
            urlopen_fn=should_not_call,
        )
    assert called is False
    assert output.read_text(encoding="utf-8") == "keep this\n"


def test_record_live_ticks_validates_tick_payload_and_keeps_partial_log(tmp_path):
    output = tmp_path / "raw.jsonl"
    sse = _sse(
        ("connected", {"symbols": ["EURUSD_otc"]}),
        ("tick", {"symbol": "EURUSD_otc", "time": "bad", "price": 1.1}),
    )
    with pytest.raises(ValueError, match="integer 'time'"):
        record_live_ticks(
            api_key="test-key",
            output_path=output,
            urlopen_fn=lambda request, timeout: io.BytesIO(sse),
        )
    records = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 1
    assert records[0]["event"] == "connected"


def test_convert_tick_log_collapses_same_second_ticks_and_applies_clock_offset(tmp_path):
    raw_path = tmp_path / "raw.jsonl"
    output_path = tmp_path / "quotes.csv"
    records = [
        {
            "schema_version": 1, "source": "otcharts", "event": "connected",
            "payload": {"symbols": ["EURUSD_otc"]},
        },
        {
            "schema_version": 1, "source": "otcharts", "event": "tick",
            "source_time_epoch_seconds": 1788103260, "payload": _tick(price=1.10001),
        },
        {
            "schema_version": 1, "source": "otcharts", "event": "tick",
            "source_time_epoch_seconds": 1788103260, "payload": _tick(price=1.10002),
        },
        {
            "schema_version": 1, "source": "otcharts", "event": "tick",
            "source_time_epoch_seconds": 1788103261, "payload": _tick(epoch=1788103261, price=1.10003),
        },
    ]
    raw_path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")

    result = convert_tick_log_to_quotes(
        raw_log_path=raw_path,
        output_path=output_path,
        symbol="EURUSD_otc",
        source_clock_ahead_seconds=7200,
    )
    with output_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))

    expected_first = datetime.fromtimestamp(1788103260 - 7200, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    assert rows[0]["timestamp"] == expected_first
    assert float(rows[0]["price"]) == pytest.approx(1.10002)
    assert float(rows[1]["price"]) == pytest.approx(1.10003)
    assert result["tick_rows_for_symbol"] == 3
    assert result["unique_second_quotes"] == 2
    assert result["same_second_ticks_collapsed"] == 1
    assert result["other_events_ignored"] == 1
    assert result["source_clock_ahead_seconds_subtracted"] == 7200
    assert result["orders_submitted"] == 0


def test_convert_tick_log_rejects_backwards_source_time_and_existing_output(tmp_path):
    raw_path = tmp_path / "raw.jsonl"
    output_path = tmp_path / "quotes.csv"
    records = [
        {"event": "tick", "source_time_epoch_seconds": 1788103262, "payload": _tick(epoch=1788103262)},
        {"event": "tick", "source_time_epoch_seconds": 1788103261, "payload": _tick(epoch=1788103261)},
    ]
    raw_path.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="moved backward"):
        convert_tick_log_to_quotes(raw_log_path=raw_path, output_path=output_path)
    assert not output_path.exists()

    raw_path.write_text(
        json.dumps({"event": "tick", "source_time_epoch_seconds": 1788103260, "payload": _tick()}) + "\n",
        encoding="utf-8",
    )
    output_path.write_text("keep this\n", encoding="utf-8")
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        convert_tick_log_to_quotes(raw_log_path=raw_path, output_path=output_path)
    assert output_path.read_text(encoding="utf-8") == "keep this\n"


def test_otc_data_cli_fails_closed_when_api_key_missing(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("OTCHARTS_API_KEY", raising=False)
    output = tmp_path / "raw.jsonl"
    code = main(["record", "--duration-seconds", "1", "--output", str(output)])
    captured = capsys.readouterr()
    assert code == 2
    assert "OTCHARTS_API_KEY" in captured.err
    assert not output.exists()



def test_converter_uses_recorded_clock_offset_and_rejects_mismatch(tmp_path):
    raw_path = tmp_path / "captured.jsonl"
    automatic_output = tmp_path / "auto.csv"
    mismatch_output = tmp_path / "mismatch.csv"
    record = {
        "event": "tick",
        "source_clock_ahead_seconds_configured": 0,
        "source_time_epoch_seconds": 1788103260,
        "payload": _tick(epoch=1788103260, price=1.12),
    }
    raw_path.write_text(json.dumps(record) + "\n", encoding="utf-8")

    result = convert_tick_log_to_quotes(
        raw_log_path=raw_path,
        output_path=automatic_output,
        source_clock_ahead_seconds=None,
    )
    with automatic_output.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    expected_timestamp = datetime.fromtimestamp(1788103260, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    assert rows[0]["timestamp"] == expected_timestamp
    assert result["source_clock_ahead_seconds_subtracted"] == 0

    with pytest.raises(ValueError, match="does not match"):
        convert_tick_log_to_quotes(
            raw_log_path=raw_path,
            output_path=mismatch_output,
            source_clock_ahead_seconds=7200,
        )
    assert not mismatch_output.exists()


def test_converter_rejects_mixed_clock_offsets_in_appended_raw_log(tmp_path):
    raw_path = tmp_path / "mixed.jsonl"
    output_path = tmp_path / "quotes.csv"
    first = {
        "event": "tick",
        "source_clock_ahead_seconds_configured": 7200,
        "source_time_epoch_seconds": 1788103260,
        "payload": _tick(epoch=1788103260, price=1.1),
    }
    second = {
        "event": "tick",
        "source_clock_ahead_seconds_configured": 0,
        "source_time_epoch_seconds": 1788103261,
        "payload": _tick(epoch=1788103261, price=1.2),
    }
    raw_path.write_text(json.dumps(first) + "\n" + json.dumps(second) + "\n", encoding="utf-8")
    with pytest.raises(ValueError, match="different clock-offset settings"):
        convert_tick_log_to_quotes(raw_log_path=raw_path, output_path=output_path)
    assert not output_path.exists()



def test_fetch_historical_candles_uses_one_read_only_request_and_writes_utc_csv(tmp_path):
    output = tmp_path / "candles.csv"
    payload = {
        "symbol": "EURUSD_otc",
        "count": 2,
        "interval": "5s",
        "gaps": {"runs": 0, "missingBars": 0},
        "candles": [
            {
                "time": 1788103265,
                "open": 1.1002,
                "high": 1.1005,
                "low": 1.1001,
                "close": 1.1004,
            },
            {
                "time": 1788103260,
                "open": 1.1000,
                "high": 1.1003,
                "low": 1.0999,
                "close": 1.1002,
            },
        ],
    }
    observed = {}

    def fake_urlopen(request, timeout):
        observed["url"] = request.full_url
        observed["authorization"] = request.get_header("Authorization")
        observed["accept"] = request.get_header("Accept")
        observed["timeout"] = timeout
        return io.BytesIO(json.dumps(payload).encode("utf-8"))

    result = fetch_historical_candles(
        api_key="secret-candle-key",
        output_path=output,
        symbol="EURUSD_otc",
        timeframe_seconds=5,
        limit=2,
        source_clock_ahead_seconds=7200,
        urlopen_fn=fake_urlopen,
    )

    with output.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert "venue=otc" in observed["url"]
    assert "symbol=EURUSD_otc" in observed["url"]
    assert "tf=5" in observed["url"]
    assert "limit=2" in observed["url"]
    assert observed["authorization"] == "Bearer secret-candle-key"
    assert observed["accept"] == "application/json"
    assert observed["timeout"] == 30
    assert "secret-candle-key" not in output.read_text(encoding="utf-8")
    assert [row["timestamp"] for row in rows] == sorted(row["timestamp"] for row in rows)
    assert rows[0]["timestamp"] == datetime.fromtimestamp(
        1788103260 - 7200, tz=timezone.utc
    ).isoformat().replace("+00:00", "Z")
    assert float(rows[0]["open"]) == pytest.approx(1.1)
    assert result["output_candles"] == 2
    assert result["rows_reordered_to_chronology"] == 1
    assert result["timeframe_seconds"] == 5
    assert result["provider_interval"] == "5s"
    assert result["provider_interval_seconds"] == 5
    assert result["observed_irregular_spacing_intervals"] == 0
    assert result["observed_large_gap_runs"] == 0
    assert result["source_clock_ahead_seconds_subtracted"] == 7200
    assert result["instrument_open"] is None
    assert result["instrument_status"] == "not_reported"
    assert result["orders_submitted"] == 0



def test_fetch_historical_candles_preserves_provider_closed_status(tmp_path):
    output = tmp_path / "closed_candles.csv"
    payload = {
        "symbol": "EURUSD_otc",
        "open": False,
        "count": 2,
        "gaps": {"runs": 0, "missingBars": 0},
        "candles": [
            {"time": 1788103260, "open": 1.1, "high": 1.2, "low": 1.0, "close": 1.1},
            {"time": 1788103320, "open": 1.1, "high": 1.2, "low": 1.0, "close": 1.1},
        ],
    }

    result = fetch_historical_candles(
        api_key="test-key",
        output_path=output,
        symbol="EURUSD_otc",
        timeframe_seconds=60,
        limit=2,
        urlopen_fn=lambda *args, **kwargs: io.BytesIO(json.dumps(payload).encode("utf-8")),
    )
    assert result["instrument_open"] is False
    assert result["instrument_status"] == "closed"
    assert "closed for trading" in result["instrument_status_note"]
    assert output.is_file()
    assert result["orders_submitted"] == 0


def test_fetch_historical_candles_rejects_malformed_provider_open_status(tmp_path):
    payload = {
        "symbol": "EURUSD_otc",
        "open": "false",
        "candles": [
            {"time": 1788103260, "open": 1.1, "high": 1.2, "low": 1.0, "close": 1.1},
        ],
    }
    with pytest.raises(ValueError, match="field 'open' must be boolean"):
        fetch_historical_candles(
            api_key="test-key",
            output_path=tmp_path / "bad_status.csv",
            symbol="EURUSD_otc",
            timeframe_seconds=60,
            limit=1,
            urlopen_fn=lambda *args, **kwargs: io.BytesIO(json.dumps(payload).encode("utf-8")),
        )
    assert not (tmp_path / "bad_status.csv").exists()



def test_fetch_historical_candles_rejects_provider_interval_mismatch(tmp_path):
    payload = {
        "symbol": "EURUSD_otc",
        "count": 2,
        "interval": "60s",
        "candles": [
            {"time": 1788103260, "open": 1.1, "high": 1.2, "low": 1.0, "close": 1.1},
            {"time": 1788103265, "open": 1.1, "high": 1.2, "low": 1.0, "close": 1.1},
        ],
    }
    with pytest.raises(ValueError, match="60s.*5s was requested"):
        fetch_historical_candles(
            api_key="test-key",
            output_path=tmp_path / "wrong_interval.csv",
            symbol="EURUSD_otc",
            timeframe_seconds=5,
            limit=2,
            urlopen_fn=lambda *args, **kwargs: io.BytesIO(json.dumps(payload).encode("utf-8")),
        )
    assert not (tmp_path / "wrong_interval.csv").exists()


def test_fetch_historical_candles_rejects_unparseable_provider_interval(tmp_path):
    payload = {
        "symbol": "EURUSD_otc",
        "interval": "M1",
        "candles": [
            {"time": 1788103260, "open": 1.1, "high": 1.2, "low": 1.0, "close": 1.1},
        ],
    }
    with pytest.raises(ValueError, match="unsupported candle interval"):
        fetch_historical_candles(
            api_key="test-key",
            output_path=tmp_path / "bad_interval.csv",
            symbol="EURUSD_otc",
            timeframe_seconds=60,
            limit=1,
            urlopen_fn=lambda *args, **kwargs: io.BytesIO(json.dumps(payload).encode("utf-8")),
        )
    assert not (tmp_path / "bad_interval.csv").exists()


def test_fetch_historical_candles_reports_spacing_gaps_in_provider_page(tmp_path):
    output = tmp_path / "gapped_candles.csv"
    payload = {
        "symbol": "EURUSD_otc",
        "interval": "5s",
        "count": 3,
        "candles": [
            {"time": 1788103260, "open": 1.1, "high": 1.2, "low": 1.0, "close": 1.1},
            {"time": 1788103265, "open": 1.1, "high": 1.2, "low": 1.0, "close": 1.1},
            {"time": 1788103280, "open": 1.1, "high": 1.2, "low": 1.0, "close": 1.1},
        ],
    }
    result = fetch_historical_candles(
        api_key="test-key",
        output_path=output,
        symbol="EURUSD_otc",
        timeframe_seconds=5,
        limit=3,
        urlopen_fn=lambda *args, **kwargs: io.BytesIO(json.dumps(payload).encode("utf-8")),
    )
    assert result["provider_interval_seconds"] == 5
    assert result["observed_irregular_spacing_intervals"] == 1
    assert result["observed_large_gap_runs"] == 1
    assert result["estimated_missing_bars_from_timestamps"] == 2



def test_otc_candles_cli_persists_fetch_report_sidecar(tmp_path, monkeypatch, capsys):
    output = tmp_path / "candles.csv"
    report_path = tmp_path / "fetch_report.json"
    monkeypatch.setenv("OTCHARTS_API_KEY", "never-print-this-key")

    from bpr02 import otc_data
    monkeypatch.setattr(
        otc_data,
        "fetch_historical_candles",
        lambda **kwargs: {
            "status": "completed",
            "output_csv": str(output),
            "output_sha256": "synthetic-test-hash",
            "instrument_status": "closed",
            "orders_submitted": 0,
        },
    )
    code = main([
        "candles", "--output", str(output), "--report-output", str(report_path)
    ])
    printed = json.loads(capsys.readouterr().out)
    saved = json.loads(report_path.read_text(encoding="utf-8"))
    assert code == 0
    assert printed["report_output_file"] == str(report_path)
    assert saved["instrument_status"] == "closed"
    assert saved["orders_submitted"] == 0
    assert "never-print-this-key" not in report_path.read_text(encoding="utf-8")


def test_otc_candles_cli_sidecar_never_overwrites_or_aliases_output(tmp_path, monkeypatch, capsys):
    output = tmp_path / "candles.csv"
    report_path = tmp_path / "fetch_report.json"
    report_path.write_text("keep me\n", encoding="utf-8")
    monkeypatch.setenv("OTCHARTS_API_KEY", "test-key")

    from bpr02 import otc_data
    def no_network(**kwargs):
        raise AssertionError("must reject paths before the HTTP request")
    monkeypatch.setattr(otc_data, "fetch_historical_candles", no_network)

    code = main(["candles", "--output", str(output), "--report-output", str(report_path)])
    assert code == 2
    assert "refusing to overwrite existing JSON report" in capsys.readouterr().err
    assert report_path.read_text(encoding="utf-8") == "keep me\n"
    assert not output.exists()

    code = main(["candles", "--output", str(output), "--report-output", str(output)])
    assert code == 2
    assert "must differ from the candle CSV output" in capsys.readouterr().err
    assert not output.exists()


def test_fetch_historical_candles_validates_request_and_refuses_overwrite(tmp_path):
    output = tmp_path / "candles.csv"
    output.write_text("preserve me\n", encoding="utf-8")
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        fetch_historical_candles(
            api_key="key",
            output_path=output,
            urlopen_fn=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not call API")),
        )
    with pytest.raises(ValueError, match="timeframe_seconds"):
        fetch_historical_candles(
            api_key="key",
            output_path=tmp_path / "bad-timeframe.csv",
            timeframe_seconds=7,
            urlopen_fn=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not call API")),
        )
    with pytest.raises(ValueError, match="limit"):
        fetch_historical_candles(
            api_key="key",
            output_path=tmp_path / "bad-limit.csv",
            limit=1501,
            urlopen_fn=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not call API")),
        )
    assert output.read_text(encoding="utf-8") == "preserve me\n"


def test_fetch_historical_candles_rejects_wrong_symbol_empty_data_and_bad_ohlc(tmp_path):
    wrong_symbol = {
        "symbol": "GBPUSD_otc",
        "candles": [{
            "time": 1788103260, "open": 1.1, "high": 1.2, "low": 1.0, "close": 1.1,
        }],
    }
    with pytest.raises(ValueError, match="not requested symbol"):
        fetch_historical_candles(
            api_key="key",
            output_path=tmp_path / "wrong-symbol.csv",
            urlopen_fn=lambda request, timeout: io.BytesIO(json.dumps(wrong_symbol).encode()),
        )

    empty = {"symbol": "EURUSD_otc", "candles": []}
    with pytest.raises(ValueError, match="no candles"):
        fetch_historical_candles(
            api_key="key",
            output_path=tmp_path / "empty.csv",
            urlopen_fn=lambda request, timeout: io.BytesIO(json.dumps(empty).encode()),
        )

    invalid = {
        "symbol": "EURUSD_otc",
        "candles": [{
            "time": 1788103260, "open": 1.1, "high": 1.0, "low": 0.9, "close": 1.05,
        }],
    }
    with pytest.raises(ValueError, match="invalid high"):
        fetch_historical_candles(
            api_key="key",
            output_path=tmp_path / "invalid.csv",
            urlopen_fn=lambda request, timeout: io.BytesIO(json.dumps(invalid).encode()),
        )


def test_otc_data_candles_cli_reads_api_key_from_environment(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("OTCHARTS_API_KEY", "api-test-key")
    output = tmp_path / "cli-candles.csv"
    payload = {
        "symbol": "EURUSD_otc",
        "candles": [{
            "time": 1788103260, "open": 1.1, "high": 1.2, "low": 1.0, "close": 1.1,
        }],
    }
    import bpr02.otc_data as otc_data
    monkeypatch.setattr(
        otc_data.urllib.request,
        "urlopen",
        lambda request, timeout: io.BytesIO(json.dumps(payload).encode("utf-8")),
    )
    code = main([
        "candles",
        "--timeframe-seconds", "5",
        "--limit", "1",
        "--output", str(output),
    ])
    result = json.loads(capsys.readouterr().out)
    assert code == 0
    assert result["mode"] == "read_only_otc_candle_fetch"
    assert result["output_candles"] == 1
    assert result["orders_submitted"] == 0
    assert output.exists()

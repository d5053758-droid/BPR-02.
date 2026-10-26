import csv
import io
import json
import os
from datetime import datetime, timezone

import pytest

from bpr02.otc_data import (
    convert_tick_log_to_quotes,
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

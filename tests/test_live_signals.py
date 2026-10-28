import io
import json
import time
from urllib.parse import urlparse, parse_qs

import pytest

from bpr02.live_signals import monitor_live_signals


def sse(event_name, payload):
    return (
        f"event: {event_name}\n"
        f"data: {json.dumps(payload, separators=(',', ':'))}\n\n"
    ).encode("utf-8")


def stream_bytes(events):
    return io.BytesIO(b"".join(sse(name, payload) for name, payload in events))


def tick(symbol, epoch, price):
    return {"symbol": symbol, "time": epoch, "price": price}


class RecordingNotifier:
    def __init__(self):
        self.events = []

    def send_event(self, event):
        self.events.append(dict(event))
        return {
            "status": "accepted_by_meta",
            "event_id": event.get("event_id", ""),
            "message_id": "wamid.synthetic-test",
            "orders_submitted": 0,
        }


def test_live_monitor_turns_mocked_sse_ticks_into_closed_m1_research_signal(tmp_path):
    symbol = "EURUSD_otc"
    offset = 7200
    first_minute = (int(time.time()) // 60) * 60 - 5 * 60
    tick_rows = [
        (1, 10.0), (10, 11.0), (20, 9.0), (59, 10.0),
        (61, 10.0), (70, 11.0), (80, 9.5), (119, 10.5),
        (121, 10.5), (130, 11.0), (140, 8.0), (179, 9.5),
        (181, 9.5),
    ]
    corrected_times = [first_minute + seconds for seconds, _ in tick_rows]
    events = [("connected", {"symbols": [symbol]})]
    events.extend(
        ("tick", tick(symbol, epoch + offset, price))
        for epoch, (_, price) in zip(corrected_times, tick_rows)
    )
    seen = {}
    response = stream_bytes(events)

    def fake_urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["authorization"] = request.get_header("Authorization")
        seen["accept"] = request.get_header("Accept")
        seen["timeout"] = timeout
        return response

    receive_times = iter(epoch + 1 for epoch in corrected_times)
    output = io.StringIO()
    event_log = tmp_path / "live-signals.jsonl"
    notifier = RecordingNotifier()
    summary = monitor_live_signals(
        api_key="test-provider-key",
        output_path=event_log,
        symbol=symbol,
        lookback=2,
        duration_seconds=300,
        source_clock_ahead_seconds=offset,
        max_tick_age_seconds=10,
        urlopen_fn=fake_urlopen,
        monotonic_fn=lambda: 0.0,
        now_epoch_fn=lambda: next(receive_times),
        stdout=output,
        whatsapp_notifier=notifier,
    )

    records = [json.loads(line) for line in output.getvalue().splitlines()]
    persisted = [json.loads(line) for line in event_log.read_text(encoding="utf-8").splitlines()]
    candles = [item for item in records if item.get("event_type") in {"research_signal", "no_trade"}]
    calls = [item for item in candles if item.get("signal") == "CALL"]

    assert urlparse(seen["url"]).path == "/v1/stream"
    query = parse_qs(urlparse(seen["url"]).query)
    assert query == {"venue": ["otc"], "symbol": [symbol]}
    assert seen["authorization"] == "Bearer test-provider-key"
    assert seen["accept"] == "text/event-stream"
    assert seen["timeout"] == 60
    assert len(candles) == 3
    assert len(calls) == 1
    assert calls[0]["event_type"] == "research_signal"
    assert calls[0]["source_mode"] == "live_stream_research_only"
    assert calls[0]["candle"]["low"] == pytest.approx(8.0)
    assert calls[0]["candle"]["close"] == pytest.approx(9.5)
    assert calls[0]["timeframe"] == "M1"
    assert calls[0]["symbol"] == symbol
    assert all(item["orders_submitted"] == 0 for item in persisted)
    assert all(item["api_key_written_to_output"] is False for item in persisted)
    assert "test-provider-key" not in event_log.read_text(encoding="utf-8")
    assert summary["connected_confirmed"] is True
    assert summary["status"] == "stream_ended_before_duration"
    assert summary["counts"]["closed_candles"] == 3
    assert summary["counts"]["signals_call"] == 1
    assert summary["partial_candle_discarded_at_stop"] is True
    assert summary["whatsapp_notifications_enabled"] is True
    assert len(notifier.events) == 1
    assert notifier.events[0]["signal"] == "CALL"
    notifications = [item for item in persisted if item.get("event_type") == "whatsapp_notification"]
    assert len(notifications) == 1
    assert notifications[0]["status"] == "accepted_by_meta"


def test_live_monitor_fails_closed_if_requested_symbol_not_subscribed(tmp_path):
    output = io.StringIO()
    summary = monitor_live_signals(
        api_key="test-key",
        output_path=tmp_path / "not-subscribed.jsonl",
        symbol="EURUSD_otc",
        duration_seconds=30,
        urlopen_fn=lambda *args, **kwargs: stream_bytes([
            ("connected", {"symbols": ["GBPUSD_otc"]}),
            ("tick", tick("GBPUSD_otc", int(time.time()) + 7200, 1.2)),
        ]),
        monotonic_fn=lambda: 0.0,
        now_epoch_fn=time.time,
        stdout=output,
    )
    records = [json.loads(line) for line in output.getvalue().splitlines()]
    assert summary["status"] == "symbol_not_subscribed"
    assert summary["connected_confirmed"] is False
    assert any(item.get("event_type") == "subscription_rejected" for item in records)
    assert not any(item.get("event_type") == "research_signal" for item in records)


def test_live_monitor_stops_for_provider_closed_event_without_signals(tmp_path):
    output = io.StringIO()
    summary = monitor_live_signals(
        api_key="test-key",
        output_path=tmp_path / "closed.jsonl",
        symbol="EURUSD_otc",
        duration_seconds=30,
        urlopen_fn=lambda *args, **kwargs: stream_bytes([
            ("closed", {"closed": ["EURUSD_otc"], "reason": "closed for trading"}),
        ]),
        monotonic_fn=lambda: 0.0,
        stdout=output,
    )
    records = [json.loads(line) for line in output.getvalue().splitlines()]
    assert summary["status"] == "instrument_closed"
    assert summary["orders_submitted"] == 0
    assert any(item.get("event_type") == "stream_stopped" for item in records)
    assert not any(item.get("signal") in {"CALL", "PUT"} for item in records)


def test_live_monitor_resets_lookback_on_a_missing_minute(tmp_path):
    symbol = "EURUSD_otc"
    offset = 7200
    first_minute = (int(time.time()) // 60) * 60 - 5 * 60
    # A jump from minute 0 directly to minute 2 cannot be repaired by inventing minute 1.
    rows = [(first_minute + 1, 10.0), (first_minute + 121, 10.5), (first_minute + 181, 10.7)]
    events = [("connected", {"symbols": [symbol]})]
    events.extend(("tick", tick(symbol, epoch + offset, price)) for epoch, price in rows)
    output = io.StringIO()
    times = iter(epoch + 1 for epoch, _ in rows)
    summary = monitor_live_signals(
        api_key="test-key",
        output_path=tmp_path / "gap.jsonl",
        symbol=symbol,
        lookback=2,
        duration_seconds=30,
        urlopen_fn=lambda *args, **kwargs: stream_bytes(events),
        monotonic_fn=lambda: 0.0,
        now_epoch_fn=lambda: next(times),
        stdout=output,
    )
    records = [json.loads(line) for line in output.getvalue().splitlines()]
    gap_records = [item for item in records if item.get("event_type") == "data_gap"]
    assert summary["counts"]["data_gaps"] == 1
    assert gap_records[0]["estimated_missing_minutes"] == 1
    assert not any(item.get("signal") in {"CALL", "PUT"} for item in records)


def test_live_monitor_discards_stale_ticks_and_never_builds_signal_from_them(tmp_path):
    symbol = "EURUSD_otc"
    offset = 7200
    now = int(time.time())
    output = io.StringIO()
    summary = monitor_live_signals(
        api_key="test-key",
        output_path=tmp_path / "stale.jsonl",
        symbol=symbol,
        duration_seconds=30,
        max_tick_age_seconds=5,
        urlopen_fn=lambda *args, **kwargs: stream_bytes([
            ("connected", {"symbols": [symbol]}),
            ("tick", tick(symbol, now - 120 + offset, 1.1)),
            ("tick", tick(symbol, now - 119 + offset, 1.2)),
        ]),
        monotonic_fn=lambda: 0.0,
        now_epoch_fn=lambda: now,
        stdout=output,
    )
    records = [json.loads(line) for line in output.getvalue().splitlines()]
    assert summary["counts"]["ticks_discarded_stale"] == 2
    assert any(item.get("event_type") == "stale_tick_discarded" for item in records)
    assert not any(item.get("signal") in {"CALL", "PUT"} for item in records)


def test_live_monitor_refuses_to_overwrite_event_log_before_network_call(tmp_path):
    event_path = tmp_path / "existing.jsonl"
    event_path.write_text("{\"keep\":true}\n", encoding="utf-8")
    called = False

    def should_not_connect(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("network must not be opened when output exists")

    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        monitor_live_signals(
            api_key="test-key",
            output_path=event_path,
            urlopen_fn=should_not_connect,
        )
    assert called is False
    assert event_path.read_text(encoding="utf-8") == "{\"keep\":true}\n"

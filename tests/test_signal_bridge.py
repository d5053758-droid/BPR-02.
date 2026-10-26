import io
import json

import pandas as pd
import pytest

from bpr02.signal_bridge import SignalBridge, run_jsonl_bridge


def payload(ts, open_, high, low, close, **extra):
    return {
        "timestamp": ts, "open": open_, "high": high, "low": low, "close": close,
        "closed": True, **extra,
    }


def test_bridge_warmup_then_emits_deterministic_research_event():
    bridge = SignalBridge(lookback=2, symbol="EURUSD", timeframe="M1", source="demo_feed")
    first = bridge.process(payload("2025-01-01T00:00:00Z", 10, 11, 9, 10))
    second = bridge.process(payload("2025-01-01T00:01:00Z", 10, 11, 9.5, 10.5))
    third = bridge.process(payload("2025-01-01T00:02:00Z", 10, 11, 9.2, 10))
    assert first["signal"] == "NO TRADE"
    assert "Warm-up" in first["reason"]
    assert second["signal"] == "NO TRADE"
    assert third["mode"] == "research_only"
    assert third["symbol"] == "EURUSD"
    assert third["timeframe"] == "M1"
    assert third["source"] == "demo_feed"
    assert third["order_sent"] is False
    assert third["manual_review_required"] is True
    assert len(third["event_id"]) == 20


def test_bridge_rejects_open_invalid_duplicate_and_backwards_candles():
    bridge = SignalBridge(lookback=2)
    with pytest.raises(ValueError, match="completed candles"):
        bridge.process({**payload("2025-01-01T00:00:00Z", 1, 2, 0, 1), "closed": False})
    bridge.process(payload("2025-01-01T00:00:00Z", 1, 2, 0, 1))
    with pytest.raises(ValueError, match="duplicate"):
        bridge.process(payload("2025-01-01T00:00:00Z", 1, 2, 0, 1))
    with pytest.raises(ValueError, match="backwards"):
        bridge.process(payload("2024-12-31T23:59:00Z", 1, 2, 0, 1))
    with pytest.raises(ValueError, match="inconsistent"):
        bridge.process(payload("2025-01-01T00:01:00Z", 1, 0.5, 0, 1))


def test_jsonl_bridge_logs_events_and_reports_bad_input(tmp_path):
    input_data = "\n".join([
        json.dumps(payload("2025-01-01T00:00:00Z", 1, 2, 0, 1)),
        "{bad json",
        json.dumps(payload("2025-01-01T00:01:00Z", 1, 2, 0, 1)),
    ]) + "\n"
    output = io.StringIO()
    event_path = tmp_path / "events.jsonl"
    processed = run_jsonl_bridge(
        io.StringIO(input_data), output, events_path=event_path,
        bridge=SignalBridge(lookback=2),
    )
    outputs = [json.loads(line) for line in output.getvalue().splitlines()]
    persisted = [json.loads(line) for line in event_path.read_text(encoding="utf-8").splitlines()]
    assert processed == 2
    assert len(persisted) == 2
    assert outputs[1]["event_type"] == "input_error"
    assert all(item["order_sent"] is False for item in persisted)


def test_history_initializes_bridge_without_emitting_historical_alerts():
    history = pd.DataFrame({
        "timestamp": pd.date_range("2025-01-01", periods=3, freq="min", tz="UTC"),
        "open": [1, 1, 1], "high": [2, 2, 2], "low": [0, 0, 0], "close": [1, 1, 1],
    })
    bridge = SignalBridge(lookback=2, history=history)
    event = bridge.process(payload("2025-01-01T00:03:00Z", 1, 2, 0, 1))
    assert event["timestamp"] == "2025-01-01T00:03:00+00:00"
    with pytest.raises(ValueError, match="chronologically sorted"):
        SignalBridge(lookback=2, history=history.iloc[::-1])

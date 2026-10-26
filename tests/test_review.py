import json

import pytest

from bpr02.review import find_event, record_review


def write_events(path):
    event = {
        "event_id": "abc123", "mode": "research_only", "timestamp": "2025-01-01T00:00:00Z",
        "symbol": "EURUSD", "timeframe": "M1", "signal": "CALL", "order_sent": False,
    }
    path.write_text(json.dumps(event) + "\n", encoding="utf-8")
    return event


def test_review_records_manual_decision_without_execution(tmp_path):
    events = tmp_path / "events.jsonl"
    journal = tmp_path / "reviews.jsonl"
    write_events(events)
    entry = record_review(
        events_path=events, event_id="abc123", decision="review",
        journal_path=journal, note="Compare candle with platform manually",
    )
    saved = json.loads(journal.read_text(encoding="utf-8").strip())
    assert entry["decision"] == "review"
    assert saved["execution_performed"] is False
    assert saved["mode"] == "manual_review_only"
    assert saved["note"] == "Compare candle with platform manually"


def test_review_rejects_missing_events_and_unknown_decisions(tmp_path):
    events = tmp_path / "events.jsonl"
    journal = tmp_path / "reviews.jsonl"
    write_events(events)
    with pytest.raises(ValueError, match="not found"):
        find_event(events, "does-not-exist")
    with pytest.raises(ValueError, match="decision"):
        record_review(events_path=events, event_id="abc123", decision="execute", journal_path=journal)

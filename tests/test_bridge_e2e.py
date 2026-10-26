import json
import subprocess
import sys

import pandas as pd

from bpr02.review import record_review


def test_replay_pipe_can_be_consumed_by_signal_bridge_and_reviewed(tmp_path):
    source = tmp_path / "candles.csv"
    event_file = tmp_path / "events.jsonl"
    review_file = tmp_path / "reviews.jsonl"
    values = [10.0, 10.2, 10.1, 9.2, 9.5, 9.4, 9.6, 9.3, 9.5, 9.7]
    highs = [11.0, 11.0, 11.0, 10.5, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0]
    lows = [9.0, 9.2, 9.1, 8.8, 9.1, 9.0, 9.2, 9.1, 9.3, 9.4]
    pd.DataFrame({
        "timestamp": pd.date_range("2025-01-01", periods=len(values), freq="min", tz="UTC"),
        "open": values,
        "high": highs,
        "low": lows,
        "close": values,
    }).to_csv(source, index=False)

    replay = subprocess.Popen(
        [
            sys.executable, "-m", "bpr02.replay", str(source),
            "--symbol", "EURUSD", "--timeframe", "M1",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        bridge = subprocess.run(
            [
                sys.executable, "-m", "bpr02.signal_bridge",
                "--lookback", "2", "--events-out", str(event_file),
            ],
            stdin=replay.stdout,
            capture_output=True,
            text=True,
            check=False,
        )
        if replay.stdout:
            replay.stdout.close()
        replay_stderr = replay.stderr.read() if replay.stderr else ""
        replay_code = replay.wait(timeout=10)
    finally:
        if replay.poll() is None:
            replay.kill()
            replay.wait(timeout=10)

    assert replay_code == 0, replay_stderr
    assert bridge.returncode == 0, bridge.stderr
    events = [json.loads(line) for line in event_file.read_text(encoding="utf-8").splitlines()]
    assert len(events) == len(values)
    assert all(event["order_sent"] is False for event in events)
    assert any(event["signal"] in {"CALL", "PUT"} for event in events)
    selected = next(event for event in events if event["signal"] in {"CALL", "PUT"})
    review = record_review(
        events_path=event_file,
        event_id=selected["event_id"],
        decision="review",
        journal_path=review_file,
        note="E2E test: manual review only",
    )
    assert review["execution_performed"] is False
    assert review_file.exists()

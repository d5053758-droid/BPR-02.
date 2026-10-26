"""Append-only manual review journal for BPR-02 alerts; no order execution."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def find_event(events_path: str | Path, event_id: str) -> dict[str, object]:
    """Find an alert by its deterministic event id in a JSON Lines log."""
    path = Path(events_path)
    if not path.is_file():
        raise FileNotFoundError(f"event log not found: {path}")
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON at event log line {line_number}") from exc
        if event.get("event_id") == event_id:
            return event
    raise ValueError(f"event_id not found: {event_id}")


def record_review(
    *,
    events_path: str | Path,
    event_id: str,
    decision: str,
    journal_path: str | Path,
    note: str = "",
) -> dict[str, object]:
    """Append a human review decision to a separate journal.

    'review' means inspect the alert manually; it does not mean an order was placed.
    'ignore' records that the alert was dismissed.
    """
    if decision not in {"review", "ignore"}:
        raise ValueError("decision must be 'review' or 'ignore'")
    event = find_event(events_path, event_id)
    if event.get("mode") != "research_only" or event.get("order_sent") is not False:
        raise ValueError("event is not a BPR-02 research-only alert")
    destination = Path(journal_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    entry: dict[str, object] = {
        "reviewed_at": datetime.now(timezone.utc).isoformat(),
        "event_id": event_id,
        "signal_timestamp": event.get("timestamp"),
        "symbol": event.get("symbol"),
        "timeframe": event.get("timeframe"),
        "signal": event.get("signal"),
        "decision": decision,
        "note": note,
        "execution_performed": False,
        "mode": "manual_review_only",
    }
    with destination.open("a", encoding="utf-8") as file:
        file.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
    return entry


def build_parser():
    parser = argparse.ArgumentParser(
        prog="bpr02-review",
        description="Record a human decision for a BPR-02 alert; this does not place an order.",
    )
    parser.add_argument("--events", required=True, help="JSONL event log path")
    parser.add_argument("--event-id", required=True, help="event_id printed by bpr02-stream")
    parser.add_argument("--decision", choices=("review", "ignore"), required=True,
                        help="'review' means inspect manually; 'ignore' dismisses the alert")
    parser.add_argument("--journal", default="bpr02_manual_reviews.jsonl", help="Append-only review journal")
    parser.add_argument("--note", default="", help="Optional note for the research journal")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        entry = record_review(
            events_path=args.events,
            event_id=args.event_id,
            decision=args.decision,
            journal_path=args.journal,
            note=args.note,
        )
        print(json.dumps(entry, ensure_ascii=False, indent=2, default=str))
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

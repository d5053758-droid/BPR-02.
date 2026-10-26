"""Consolidated data-readiness preflight for research-only BPR-02 runs."""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

from .audit import audit_csv_file
from .provenance import sha256_file
from .quote_audit import audit_quote_series
from .subminute import load_quote_csv


def _finite_number(value: object, name: str, *, minimum: float, inclusive: bool) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be a finite number")
    valid = number >= minimum if inclusive else number > minimum
    if not valid:
        operator = ">=" if inclusive else ">"
        raise ValueError(f"{name} must be {operator} {minimum}")
    return number


def assess_readiness(
    candle_audit: dict[str, Any],
    *,
    expected_interval_seconds: float | None,
    quote_audit: dict[str, Any] | None = None,
    max_missing_bars: int = 0,
    expiry_seconds: float = 3.0,
    target_resolution_fraction: float = 0.25,
    max_quote_p95_gap_seconds: float | None = None,
    max_quote_delay_seconds: float = 1.0,
    bar_timestamp_label: str = "open",
) -> dict[str, Any]:
    """Combine candle integrity, explicit cadence, gaps and optional quote quality.

    Passing this screen only means the files meet the configured data checks.
    It does not establish feed provenance, Pocket Option OTC price parity,
    exact execution/settlement reconstruction, or profitability.
    """
    expiry = _finite_number(expiry_seconds, "expiry_seconds", minimum=0, inclusive=False)
    fraction = _finite_number(
        target_resolution_fraction, "target_resolution_fraction", minimum=0, inclusive=False
    )
    if fraction > 1:
        raise ValueError("target_resolution_fraction must be <= 1")
    delay = _finite_number(
        max_quote_delay_seconds, "max_quote_delay_seconds", minimum=0, inclusive=True
    )
    if max_quote_p95_gap_seconds is not None:
        quote_threshold = _finite_number(
            max_quote_p95_gap_seconds,
            "max_quote_p95_gap_seconds",
            minimum=0,
            inclusive=True,
        )
        threshold_source = "explicit"
    else:
        quote_threshold = expiry * fraction
        threshold_source = "expiry_fraction_heuristic"
    if isinstance(max_missing_bars, bool) or not isinstance(max_missing_bars, int) or max_missing_bars < 0:
        raise ValueError("max_missing_bars must be an integer >= 0")
    if bar_timestamp_label not in ("open", "close"):
        raise ValueError("bar_timestamp_label must be 'open' or 'close'")

    gates: dict[str, dict[str, Any]] = {}

    def gate(name: str, passed: bool, detail: str) -> None:
        gates[name] = {"passed": bool(passed), "required": True, "detail": detail}

    rows = int(candle_audit.get("rows", 0) or 0)
    structurally_valid = bool(candle_audit.get("structurally_valid", False))
    interval = candle_audit.get("expected_interval_seconds")
    interval_source = candle_audit.get("interval_source", "unavailable")
    explicit_interval = (
        expected_interval_seconds is not None
        and interval_source == "provided"
        and interval is not None
    )
    large_gap_count = int(candle_audit.get("gaps_over_1_5x_interval", 0) or 0)
    estimated_missing = int(candle_audit.get("estimated_missing_bars", 0) or 0)
    irregular = int(candle_audit.get("irregular_intervals", 0) or 0)
    unexplained_irregular = max(irregular - large_gap_count, 0)

    gate(
        "minimum_candle_history",
        rows >= 2,
        f"{rows} candle row(s); at least two are required to inspect timestamp spacing.",
    )
    gate(
        "candle_structure",
        structurally_valid,
        "Raw timestamps and OHLC ranges pass structural checks."
        if structurally_valid
        else "Structural audit found missing/invalid timestamps, duplicate/out-of-order rows, invalid OHLC rows, or an empty dataset.",
    )
    gate(
        "explicit_candle_interval",
        explicit_interval,
        (
            f"Expected cadence is explicitly configured as {float(interval):g} seconds."
            if explicit_interval
            else "Pass --expected-interval-seconds matching the source timeframe; inferred cadence cannot detect systematic missing bars."
        ),
    )
    gap_passed = estimated_missing <= max_missing_bars and unexplained_irregular == 0
    gate(
        "candle_gaps_within_budget",
        gap_passed,
        (
            f"Estimated missing bars: {estimated_missing}; permitted: {max_missing_bars}; "
            f"large gaps: {large_gap_count}; unexplained irregular intervals: {unexplained_irregular}."
        ),
    )

    quote_coverage: dict[str, Any] | None = None
    mode = "bar_based_research"
    if quote_audit is not None:
        mode = "seconds_expiry_research"
        measured_p95 = quote_audit.get("p95_gap_seconds")
        resolution_passed = (
            measured_p95 is not None
            and math.isfinite(float(measured_p95))
            and float(measured_p95) <= quote_threshold
        )
        gate(
            "quote_temporal_resolution",
            resolution_passed,
            (
                f"Measured p95 quote gap: {measured_p95!r} seconds; "
                f"screening threshold: {quote_threshold:g} seconds "
                f"({threshold_source})."
            ),
        )

        quote_start = pd.Timestamp(quote_audit["start_timestamp"]) if quote_audit.get("start_timestamp") else None
        quote_end = pd.Timestamp(quote_audit["end_timestamp"]) if quote_audit.get("end_timestamp") else None
        candle_start = pd.Timestamp(candle_audit["start_timestamp"]) if candle_audit.get("start_timestamp") else None
        candle_end = pd.Timestamp(candle_audit["end_timestamp"]) if candle_audit.get("end_timestamp") else None
        try:
            interval_seconds = float(interval) if interval is not None else None
        except (TypeError, ValueError):
            interval_seconds = None

        required_quote_start = candle_start
        required_quote_end = None
        if candle_end is not None and interval_seconds is not None and interval_seconds > 0:
            last_signal_available_at = candle_end
            if bar_timestamp_label == "open":
                last_signal_available_at += pd.to_timedelta(interval_seconds, unit="s")
            required_quote_end = (
                last_signal_available_at
                + pd.to_timedelta(expiry + delay, unit="s")
            )

        start_covered = (
            quote_start is not None
            and required_quote_start is not None
            and quote_start <= required_quote_start
        )
        end_covered = (
            quote_end is not None
            and required_quote_end is not None
            and quote_end >= required_quote_end
        )
        quote_coverage = {
            "quote_start_timestamp": quote_start.isoformat() if quote_start is not None else None,
            "quote_end_timestamp": quote_end.isoformat() if quote_end is not None else None,
            "required_start_timestamp": required_quote_start.isoformat() if required_quote_start is not None else None,
            "required_end_timestamp": required_quote_end.isoformat() if required_quote_end is not None else None,
            "start_covered": start_covered,
            "settlement_horizon_covered": end_covered,
            "coverage_is_heuristic": True,
            "note": "Coverage compares file time ranges only; it does not prove the quotes are from the intended market or settlement feed.",
        }
        gate(
            "quote_covers_candle_start",
            start_covered,
            "Quote data begin at or before the first candle timestamp."
            if start_covered
            else "Quote history begins after the first candle timestamp, or timestamps are unavailable.",
        )
        gate(
            "quote_covers_settlement_horizon",
            end_covered,
            "Quote data extend through the last candle's modeled signal-availability time plus expiry and quote-delay tolerance."
            if end_covered
            else "Quote history does not extend through the required final settlement horizon, or the candle interval/timestamps are unavailable.",
        )

    required_passed = all(item["passed"] for item in gates.values() if item["required"])
    if not required_passed:
        status = "not_ready"
    elif mode == "seconds_expiry_research":
        status = "ready_for_seconds_expiry_research"
    else:
        status = "ready_for_bar_based_research"

    return {
        "mode": "dataset_readiness_preflight",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "research_mode": mode,
        "readiness_status": status,
        "overall_ready": required_passed,
        "gates": gates,
        "parameters": {
            "expected_interval_seconds": expected_interval_seconds,
            "max_missing_bars": max_missing_bars,
            "expiry_seconds": expiry,
            "target_resolution_fraction": fraction,
            "max_quote_p95_gap_seconds": max_quote_p95_gap_seconds,
            "effective_quote_p95_threshold_seconds": quote_threshold if quote_audit is not None else None,
            "quote_threshold_source": threshold_source if quote_audit is not None else None,
            "max_quote_delay_seconds": delay,
            "bar_timestamp_label": bar_timestamp_label,
        },
        "candle_quality": candle_audit,
        "quote_quality": quote_audit,
        "quote_coverage": quote_coverage,
        "market_feed_verified": False,
        "broker_quote_parity_verified": False,
        "profitability_assessed": False,
        "orders_submitted": 0,
        "limitations": [
            "A successful preflight is a data-quality screen, not a strategy backtest or profitability claim.",
            "Inferred candle cadence cannot detect systematic omission of bars; configure the actual source interval.",
            "Quote timestamp coverage and sampling resolution do not prove Pocket Option OTC price or settlement parity.",
            "For second-expiry research, 5-second OHLC candles alone cannot reconstruct exact 3-second entry or settlement prices.",
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bpr02-readiness",
        description="Preflight candle quality and optional quote coverage before research backtests.",
    )
    parser.add_argument("candles_csv", help="Historical OHLC CSV")
    parser.add_argument("--quotes-csv", help="Optional timestamped quote CSV with timestamp and price columns")
    parser.add_argument(
        "--expected-interval-seconds",
        type=float,
        help="Required source candle spacing, e.g. 60 for M1 or 5 for five-second candles",
    )
    parser.add_argument("--max-missing-bars", type=int, default=0, help="Allowed estimated missing candle slots (default 0)")
    parser.add_argument("--expiry-seconds", type=float, default=3.0)
    parser.add_argument(
        "--target-resolution-fraction",
        type=float,
        default=0.25,
        help="Default quote p95 screen as a fraction of expiry when no explicit p95 limit is given (default 0.25)",
    )
    parser.add_argument(
        "--max-quote-p95-gap-seconds",
        type=float,
        help="Optional explicit p95 quote-gap limit; otherwise expiry * target-resolution-fraction is used",
    )
    parser.add_argument("--max-quote-delay-seconds", type=float, default=1.0)
    parser.add_argument("--bar-timestamp-label", choices=("open", "close"), default="open")
    parser.add_argument("--output", default="bpr02_readiness.json", help="Readiness report JSON output path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    candle_path = Path(args.candles_csv)
    quote_path = Path(args.quotes_csv) if args.quotes_csv else None
    output_path = Path(args.output)
    input_paths = {candle_path.resolve()}
    if quote_path is not None:
        input_paths.add(quote_path.resolve())
    if quote_path is not None and quote_path.resolve() == candle_path.resolve():
        print("ERROR: candles and quotes must be different files", file=sys.stderr)
        return 2
    if output_path.resolve() in input_paths:
        print("ERROR: report output must not overwrite either input CSV", file=sys.stderr)
        return 2

    try:
        if args.expected_interval_seconds is not None:
            _finite_number(
                args.expected_interval_seconds,
                "expected_interval_seconds",
                minimum=0,
                inclusive=False,
            )
        if isinstance(args.max_missing_bars, bool) or args.max_missing_bars < 0:
            raise ValueError("--max-missing-bars must be an integer >= 0")
        if not math.isfinite(args.expiry_seconds) or args.expiry_seconds <= 0:
            raise ValueError("--expiry-seconds must be finite and > 0")
        if not math.isfinite(args.target_resolution_fraction) or not 0 < args.target_resolution_fraction <= 1:
            raise ValueError("--target-resolution-fraction must be in (0, 1]")
        if args.max_quote_p95_gap_seconds is not None and (
            not math.isfinite(args.max_quote_p95_gap_seconds) or args.max_quote_p95_gap_seconds < 0
        ):
            raise ValueError("--max-quote-p95-gap-seconds must be finite and >= 0")
        if not math.isfinite(args.max_quote_delay_seconds) or args.max_quote_delay_seconds < 0:
            raise ValueError("--max-quote-delay-seconds must be finite and >= 0")

        candle_audit = audit_csv_file(
            candle_path,
            expected_interval_seconds=args.expected_interval_seconds,
        )
        quote_audit = None
        if quote_path is not None:
            quotes = load_quote_csv(quote_path)
            quote_audit = audit_quote_series(
                quotes,
                expiry_seconds=args.expiry_seconds,
                target_resolution_fraction=args.target_resolution_fraction,
            )
            quote_audit["source_file"] = quote_path.name
            quote_audit["source_sha256"] = sha256_file(quote_path)
            quote_audit["source_bytes"] = quote_path.stat().st_size

        report = assess_readiness(
            candle_audit,
            expected_interval_seconds=args.expected_interval_seconds,
            quote_audit=quote_audit,
            max_missing_bars=args.max_missing_bars,
            expiry_seconds=args.expiry_seconds,
            target_resolution_fraction=args.target_resolution_fraction,
            max_quote_p95_gap_seconds=args.max_quote_p95_gap_seconds,
            max_quote_delay_seconds=args.max_quote_delay_seconds,
            bar_timestamp_label=args.bar_timestamp_label,
        )
        report["source_files"] = {
            "candles": {
                "file": candle_path.name,
                "sha256": sha256_file(candle_path),
                "bytes": candle_path.stat().st_size,
            },
            "quotes": (
                {
                    "file": quote_path.name,
                    "sha256": sha256_file(quote_path),
                    "bytes": quote_path.stat().st_size,
                }
                if quote_path is not None
                else None
            ),
        }
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False, default=str) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False, default=str))
        return 0 if report["overall_ready"] else 3
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

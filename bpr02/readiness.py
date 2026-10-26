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
    provider_report: dict[str, Any] | None = None,
    candle_source_sha256: str | None = None,
    expected_symbol: str = "EURUSD_otc",
    max_missing_bars: int = 0,
    lookback: int = 12,
    expiry_bars: int = 1,
    expiry_seconds: float = 3.0,
    target_resolution_fraction: float = 0.25,
    max_quote_p95_gap_seconds: float | None = None,
    max_quote_gap_seconds: float | None = None,
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
    if max_quote_gap_seconds is not None:
        max_quote_gap = _finite_number(
            max_quote_gap_seconds,
            "max_quote_gap_seconds",
            minimum=0,
            inclusive=True,
        )
        quote_gap_threshold_source = "explicit"
    else:
        max_quote_gap = None
        quote_gap_threshold_source = "not_configured"
    if isinstance(max_missing_bars, bool) or not isinstance(max_missing_bars, int) or max_missing_bars < 0:
        raise ValueError("max_missing_bars must be an integer >= 0")
    if isinstance(lookback, bool) or not isinstance(lookback, int) or lookback < 2:
        raise ValueError("lookback must be an integer >= 2")
    if isinstance(expiry_bars, bool) or not isinstance(expiry_bars, int) or expiry_bars < 1:
        raise ValueError("expiry_bars must be an integer >= 1")
    if bar_timestamp_label not in ("open", "close"):
        raise ValueError("bar_timestamp_label must be 'open' or 'close'")
    if not isinstance(expected_symbol, str) or not expected_symbol.strip():
        raise ValueError("expected_symbol must be a non-empty string")

    gates: dict[str, dict[str, Any]] = {}

    def gate(
        name: str,
        passed: bool | None,
        detail: str,
        *,
        required: bool = True,
    ) -> None:
        gates[name] = {"passed": passed, "required": required, "detail": detail}

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
    required_history_rows = lookback + 1 if quote_audit is not None else lookback + expiry_bars + 1
    gate(
        "minimum_strategy_history",
        rows >= required_history_rows,
        (
            f"{rows} candle row(s); this configuration needs at least {required_history_rows} "
            f"to provide lookback={lookback} and "
            + (
                "at least one candidate-signal candle for quote-based expiry research."
                if quote_audit is not None
                else f"expiry_bars={expiry_bars} after a candidate-signal candle."
            )
        ),
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

    provider_quality: dict[str, Any] | None = None
    if provider_report is not None:
        report_mode_ok = provider_report.get("mode") == "read_only_otc_candle_fetch"
        report_hash = provider_report.get("output_sha256")
        hash_matches_csv = (
            isinstance(report_hash, str)
            and isinstance(candle_source_sha256, str)
            and report_hash == candle_source_sha256
        )
        provider_symbol = provider_report.get("symbol")
        provider_venue = provider_report.get("venue")
        instrument_matches = (
            provider_venue == "otc"
            and provider_symbol == expected_symbol
        )
        provider_interval = provider_report.get("timeframe_seconds")
        interval_matches = (
            expected_interval_seconds is not None
            and not isinstance(provider_interval, bool)
            and isinstance(provider_interval, (int, float))
            and math.isfinite(float(provider_interval))
            and float(provider_interval) == float(expected_interval_seconds)
        )

        provider_gaps = provider_report.get("provider_gaps")
        gap_metadata_valid = (
            isinstance(provider_gaps, dict)
            and isinstance(provider_gaps.get("runs"), int)
            and not isinstance(provider_gaps.get("runs"), bool)
            and provider_gaps.get("runs") >= 0
            and isinstance(provider_gaps.get("missingBars"), int)
            and not isinstance(provider_gaps.get("missingBars"), bool)
            and provider_gaps.get("missingBars") >= 0
        )
        provider_gap_runs = provider_gaps.get("runs") if gap_metadata_valid else None
        provider_missing_bars = provider_gaps.get("missingBars") if gap_metadata_valid else None
        provider_gaps_within_budget = (
            gap_metadata_valid and provider_missing_bars <= max_missing_bars
        )

        gate(
            "provider_report_type",
            report_mode_ok,
            "Acquisition report identifies a BPR-02 read-only OTC candle fetch."
            if report_mode_ok
            else "The supplied JSON is not a recognized BPR-02 OTC candle acquisition report.",
        )
        gate(
            "provider_report_matches_candle_csv",
            hash_matches_csv,
            "The acquisition report's output SHA-256 matches the exact candle CSV being audited."
            if hash_matches_csv
            else "The acquisition report hash is missing or does not match this candle CSV.",
        )
        gate(
            "provider_instrument_matches_expected",
            instrument_matches,
            f"Reported venue/symbol: {provider_venue!r}/{provider_symbol!r}; expected: 'otc'/{expected_symbol!r}.",
        )
        gate(
            "provider_interval_matches_expected",
            interval_matches,
            f"Provider timeframe: {provider_interval!r}; expected source interval: {expected_interval_seconds!r} seconds.",
        )
        gate(
            "provider_gap_metadata_within_budget",
            provider_gaps_within_budget,
            (
                f"Provider reports {provider_gap_runs} gap run(s) and {provider_missing_bars} missing bar(s); "
                f"allowed missing bars: {max_missing_bars}."
                if gap_metadata_valid
                else "Provider gap metadata must contain non-negative integer 'runs' and 'missingBars' fields."
            ),
        )
        provider_quality = {
            "mode": provider_report.get("mode"),
            "source": provider_report.get("source"),
            "venue": provider_venue,
            "symbol": provider_symbol,
            "timeframe_seconds": provider_interval,
            "instrument_status": provider_report.get("instrument_status", "not_reported"),
            "provider_gaps": provider_gaps,
            "provider_output_sha256": report_hash,
            "output_sha256_matches_csv": hash_matches_csv,
            "report_type_recognized": report_mode_ok,
            "instrument_matches_expected": instrument_matches,
            "interval_matches_expected": interval_matches,
            "gap_metadata_valid": gap_metadata_valid,
            "provider_gap_metadata_within_budget": provider_gaps_within_budget,
            "note": "This validates consistency of local report metadata and file hashes; it is not a cryptographic provider attestation.",
        }

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
        observed_max_gap = quote_audit.get("max_gap_seconds")
        if max_quote_gap is None:
            max_gap_passed = None
            max_gap_detail = (
                f"Observed maximum gap: {observed_max_gap!r} seconds; "
                "no explicit maximum-gap limit configured, so this gate is reported but not enforced."
            )
        else:
            max_gap_passed = (
                observed_max_gap is not None
                and math.isfinite(float(observed_max_gap))
                and float(observed_max_gap) <= max_quote_gap
            )
            max_gap_detail = (
                f"Observed maximum gap: {observed_max_gap!r} seconds; "
                f"explicit limit: {max_quote_gap:g} seconds."
            )
        gate(
            "quote_maximum_gap",
            max_gap_passed,
            max_gap_detail,
            required=max_quote_gap is not None,
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

    required_passed = all(item["passed"] is True for item in gates.values() if item["required"])
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
            "expected_symbol": expected_symbol,
            "provider_report_supplied": provider_report is not None,
            "max_missing_bars": max_missing_bars,
            "lookback": lookback,
            "expiry_bars": expiry_bars,
            "minimum_required_candle_rows": required_history_rows,
            "expiry_seconds": expiry,
            "target_resolution_fraction": fraction,
            "max_quote_p95_gap_seconds": max_quote_p95_gap_seconds,
            "max_quote_gap_seconds": max_quote_gap_seconds,
            "quote_max_gap_threshold_source": quote_gap_threshold_source,
            "effective_quote_p95_threshold_seconds": quote_threshold if quote_audit is not None else None,
            "quote_threshold_source": threshold_source if quote_audit is not None else None,
            "max_quote_delay_seconds": delay,
            "bar_timestamp_label": bar_timestamp_label,
        },
        "candle_quality": candle_audit,
        "provider_quality": provider_quality,
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
        "--provider-report",
        help="Optional JSON sidecar from bpr02-otc-data candles; verifies its hash, symbol, timeframe and gap metadata against this CSV",
    )
    parser.add_argument(
        "--expected-symbol",
        default="EURUSD_otc",
        help="Expected symbol when --provider-report is supplied (default EURUSD_otc)",
    )
    parser.add_argument(
        "--expected-interval-seconds",
        type=float,
        help="Required source candle spacing, e.g. 60 for M1 or 5 for five-second candles",
    )
    parser.add_argument("--max-missing-bars", type=int, default=0, help="Allowed estimated missing candle slots (default 0)")
    parser.add_argument("--lookback", type=int, default=12, help="Strategy lookback in candles (default 12)")
    parser.add_argument("--expiry-bars", type=int, default=1, help="Bar-backtest expiry in candles (default 1; ignored for quote-based settlement)")
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
    parser.add_argument(
        "--max-quote-gap-seconds",
        type=float,
        help="Optional hard limit for the largest quote gap; unlike p95, this also screens rare long gaps",
    )
    parser.add_argument("--max-quote-delay-seconds", type=float, default=1.0)
    parser.add_argument("--bar-timestamp-label", choices=("open", "close"), default="open")
    parser.add_argument("--output", default="bpr02_readiness.json", help="Readiness report JSON output path")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    candle_path = Path(args.candles_csv)
    quote_path = Path(args.quotes_csv) if args.quotes_csv else None
    provider_report_path = Path(args.provider_report) if args.provider_report else None
    output_path = Path(args.output)
    input_paths = {candle_path.resolve()}
    if quote_path is not None:
        input_paths.add(quote_path.resolve())
    if provider_report_path is not None:
        input_paths.add(provider_report_path.resolve())
    if quote_path is not None and quote_path.resolve() == candle_path.resolve():
        print("ERROR: candles and quotes must be different files", file=sys.stderr)
        return 2
    if provider_report_path is not None and provider_report_path.resolve() in {
        candle_path.resolve(), *( [quote_path.resolve()] if quote_path is not None else [] )
    }:
        print("ERROR: provider report must differ from the candle and quote CSV files", file=sys.stderr)
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
        if args.lookback < 2:
            raise ValueError("--lookback must be >= 2")
        if args.expiry_bars < 1:
            raise ValueError("--expiry-bars must be >= 1")
        if not math.isfinite(args.expiry_seconds) or args.expiry_seconds <= 0:
            raise ValueError("--expiry-seconds must be finite and > 0")
        if not math.isfinite(args.target_resolution_fraction) or not 0 < args.target_resolution_fraction <= 1:
            raise ValueError("--target-resolution-fraction must be in (0, 1]")
        if args.max_quote_p95_gap_seconds is not None and (
            not math.isfinite(args.max_quote_p95_gap_seconds) or args.max_quote_p95_gap_seconds < 0
        ):
            raise ValueError("--max-quote-p95-gap-seconds must be finite and >= 0")
        if args.max_quote_gap_seconds is not None and (
            not math.isfinite(args.max_quote_gap_seconds) or args.max_quote_gap_seconds < 0
        ):
            raise ValueError("--max-quote-gap-seconds must be finite and >= 0")
        if not math.isfinite(args.max_quote_delay_seconds) or args.max_quote_delay_seconds < 0:
            raise ValueError("--max-quote-delay-seconds must be finite and >= 0")

        provider_report = None
        if provider_report_path is not None:
            provider_report = json.loads(provider_report_path.read_text(encoding="utf-8"))
            if not isinstance(provider_report, dict):
                raise ValueError("--provider-report must contain a JSON object")

        candle_sha256 = sha256_file(candle_path)
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
            provider_report=provider_report,
            candle_source_sha256=candle_sha256,
            expected_symbol=args.expected_symbol,
            max_missing_bars=args.max_missing_bars,
            lookback=args.lookback,
            expiry_bars=args.expiry_bars,
            expiry_seconds=args.expiry_seconds,
            target_resolution_fraction=args.target_resolution_fraction,
            max_quote_p95_gap_seconds=args.max_quote_p95_gap_seconds,
            max_quote_gap_seconds=args.max_quote_gap_seconds,
            max_quote_delay_seconds=args.max_quote_delay_seconds,
            bar_timestamp_label=args.bar_timestamp_label,
        )
        report["source_files"] = {
            "candles": {
                "file": candle_path.name,
                "sha256": candle_sha256,
                "bytes": candle_path.stat().st_size,
            },
            "provider_report": (
                {
                    "file": provider_report_path.name,
                    "sha256": sha256_file(provider_report_path),
                    "bytes": provider_report_path.stat().st_size,
                }
                if provider_report_path is not None
                else None
            ),
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

"""Preflight quality audit for timestamped quote series used in short-expiry research."""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .provenance import sha256_file
from .subminute import load_quote_csv


def _positive_finite(value: object, name: str, *, allow_zero: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    number = float(value)
    if not math.isfinite(number) or (number < 0 if allow_zero else number <= 0):
        comparator = ">= 0" if allow_zero else "> 0"
        raise ValueError(f"{name} must be finite and {comparator}")
    return number


def audit_quote_series(
    quotes: pd.DataFrame,
    *,
    expiry_seconds: float = 3.0,
    target_resolution_fraction: float = 0.25,
) -> dict[str, object]:
    """Describe sampling resolution and temporal gaps, without scoring profitability.

    The target fraction is a configurable screening heuristic (default p95 gap
    target: one quarter of the requested expiry). It is not a universal trading
    requirement, a latency model, or evidence that this feed matches a broker.
    """
    expiry = _positive_finite(expiry_seconds, "expiry_seconds")
    fraction = _positive_finite(
        target_resolution_fraction, "target_resolution_fraction"
    )
    if fraction > 1:
        raise ValueError("target_resolution_fraction must be <= 1")

    required = {"timestamp", "price"}
    missing = sorted(required.difference(quotes.columns))
    if missing:
        raise ValueError(f"quotes must contain {sorted(required)}; missing {missing}")
    if quotes.empty:
        raise ValueError("quotes contain no observations")
    frame = quotes.copy().reset_index(drop=True)
    try:
        parsed = pd.to_datetime(frame["timestamp"], errors="raise", utc=True, format="mixed")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"quote timestamps are invalid: {exc}") from exc
    if parsed.isna().any():
        raise ValueError("quote timestamps must not be missing")
    if parsed.duplicated().any():
        raise ValueError("quote timestamps must be unique")
    if not parsed.is_monotonic_increasing:
        raise ValueError("quote timestamps must be chronologically sorted")
    prices = pd.to_numeric(frame["price"], errors="coerce")
    if prices.isna().any() or not prices.map(lambda value: math.isfinite(float(value))).all():
        raise ValueError("quote prices must be numeric and finite")
    if (prices <= 0).any():
        raise ValueError("quote prices must be greater than zero")

    gaps = parsed.diff().dt.total_seconds().dropna()
    span_seconds = float((parsed.iloc[-1] - parsed.iloc[0]).total_seconds()) if len(parsed) >= 2 else 0.0
    target_gap = expiry * fraction
    p50 = float(gaps.quantile(0.50)) if not gaps.empty else None
    p95 = float(gaps.quantile(0.95)) if not gaps.empty else None
    maximum = float(gaps.max()) if not gaps.empty else None
    target_miss_count = int((gaps > target_gap).sum())
    half_expiry_miss_count = int((gaps > expiry / 2).sum())

    if len(parsed) < 2:
        label = "insufficient_observations"
    elif span_seconds < expiry:
        label = "insufficient_time_coverage"
    elif p95 is not None and p95 <= target_gap:
        label = "meets_configured_resolution_screen"
    elif p95 is not None and p95 <= expiry / 2:
        label = "limited_resolution_review_quote_delays"
    else:
        label = "low_temporal_resolution_for_requested_expiry"

    return {
        "mode": "quote_temporal_quality_audit",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "observations": int(len(parsed)),
        "start_timestamp": parsed.iloc[0].isoformat(),
        "end_timestamp": parsed.iloc[-1].isoformat(),
        "span_seconds": span_seconds,
        "expiry_seconds": expiry,
        "target_resolution_fraction_of_expiry": fraction,
        "target_p95_gap_seconds": target_gap,
        "gap_count": int(len(gaps)),
        "median_gap_seconds": p50,
        "p95_gap_seconds": p95,
        "max_gap_seconds": maximum,
        "fraction_gaps_within_target": (
            float(1.0 - (target_miss_count / len(gaps))) if not gaps.empty else None
        ),
        "gaps_over_target_count": target_miss_count,
        "gaps_over_half_expiry_count": half_expiry_miss_count,
        "temporal_resolution_assessment": label,
        "assessment_is_heuristic": True,
        "profitability_assessed": False,
        "broker_quote_parity_verified": False,
        "note": (
            "Temporal sampling diagnostic only. It does not model execution latency, "
            "validate settlement rules, establish Pocket Option OTC quote parity, or "
            "demonstrate strategy profitability."
        ),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bpr02-quote-audit",
        description="Audit timestamps and sampling resolution for historical quote CSV data.",
    )
    parser.add_argument("quotes_csv", help="CSV with timezone-aware timestamp and positive price columns")
    parser.add_argument("--expiry-seconds", type=float, default=3.0)
    parser.add_argument(
        "--target-resolution-fraction",
        type=float,
        default=0.25,
        help="Screening heuristic: target p95 gap as a fraction of expiry (default 0.25)",
    )
    parser.add_argument(
        "--max-p95-gap-seconds",
        type=float,
        help="Optional explicit maximum p95 quote interval; audit exits with status 3 if exceeded",
    )
    parser.add_argument("--output", default="bpr02_quote_audit.json")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    source = Path(args.quotes_csv)
    output = Path(args.output)
    if source.resolve() == output.resolve():
        print("ERROR: audit output must not overwrite the quote CSV", file=sys.stderr)
        return 2

    try:
        if args.max_p95_gap_seconds is not None:
            _positive_finite(
                args.max_p95_gap_seconds, "max_p95_gap_seconds", allow_zero=True
            )
        quotes = load_quote_csv(source)
        report = audit_quote_series(
            quotes,
            expiry_seconds=args.expiry_seconds,
            target_resolution_fraction=args.target_resolution_fraction,
        )
        report["source_file"] = source.name
        report["source_sha256"] = sha256_file(source)
        report["source_bytes"] = source.stat().st_size
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False))
        if (
            args.max_p95_gap_seconds is not None
            and report["p95_gap_seconds"] is not None
            and float(report["p95_gap_seconds"]) > args.max_p95_gap_seconds
        ):
            print(
                f"QUALITY THRESHOLD NOT MET: p95 gap {report['p95_gap_seconds']}s exceeds "
                f"{args.max_p95_gap_seconds}s",
                file=sys.stderr,
            )
            return 3
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

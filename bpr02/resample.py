"""Quality-aware OHLC resampling for historical research datasets."""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import pandas as pd

from .data import load_ohlc_csv
from .provenance import build_run_manifest, sha256_file, write_run_manifest


def _positive_int(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def resample_ohlc_to_interval(
    candles: pd.DataFrame,
    *,
    source_interval_seconds: int = 5,
    target_interval_seconds: int = 60,
    max_gap_examples: int = 20,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Aggregate only target bars with every expected source candle present.

    Timestamps identify source-candle opens. The output timestamp identifies
    the target-candle open. Incomplete target intervals are excluded from OHLC
    output and documented in the report; missing prices are never fabricated.
    """
    source_interval = _positive_int(source_interval_seconds, "source_interval_seconds")
    target_interval = _positive_int(target_interval_seconds, "target_interval_seconds")
    if target_interval <= source_interval or target_interval % source_interval:
        raise ValueError(
            "target_interval_seconds must be greater than and divisible by source_interval_seconds"
        )
    if isinstance(max_gap_examples, bool) or not isinstance(max_gap_examples, int) or max_gap_examples < 0:
        raise ValueError("max_gap_examples must be an integer >= 0")
    required = {"timestamp", "open", "high", "low", "close"}
    missing = sorted(required.difference(candles.columns))
    if missing:
        raise ValueError(f"candles must contain {sorted(required)}; missing {missing}")
    if candles.empty:
        raise ValueError("input candles must contain at least one row")

    frame = candles.loc[:, ["timestamp", "open", "high", "low", "close"]].copy()
    timestamps = pd.to_datetime(frame["timestamp"], errors="raise", utc=True)
    if timestamps.isna().any():
        raise ValueError("timestamps must not contain missing values")
    if timestamps.duplicated().any():
        raise ValueError("source candle timestamps must be unique")
    if not timestamps.is_monotonic_increasing:
        raise ValueError("source candles must be chronologically sorted")

    for column in ("open", "high", "low", "close"):
        try:
            values = pd.to_numeric(frame[column], errors="raise").astype(float)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{column} prices must be numeric and finite") from exc
        if values.isna().any() or not values.map(math.isfinite).all():
            raise ValueError(f"{column} prices must be numeric and finite")
        frame[column] = values

    bad_high = frame["high"] < frame[["open", "close", "low"]].max(axis=1)
    bad_low = frame["low"] > frame[["open", "close", "high"]].min(axis=1)
    if (bad_high | bad_low).any():
        rows = [index + 1 for index, bad in enumerate((bad_high | bad_low).to_numpy()) if bad][:5]
        raise ValueError(f"invalid OHLC candle range at input row(s): {rows}")

    source_ns = source_interval * 1_000_000_000
    if (timestamps.astype("int64") % source_ns != 0).any():
        raise ValueError(
            f"source timestamps must align to the {source_interval}-second UTC grid"
        )
    frame["timestamp"] = timestamps
    frame["target_bucket"] = timestamps.dt.floor(f"{target_interval}s")

    expected_per_target = target_interval // source_interval
    source_step = pd.Timedelta(seconds=source_interval)
    target_step = pd.Timedelta(seconds=target_interval)
    output_rows: list[dict[str, Any]] = []
    incomplete_examples: list[dict[str, Any]] = []
    incomplete_buckets = 0
    missing_slots_in_partial_buckets = 0

    for bucket, group in frame.groupby("target_bucket", sort=True):
        group = group.sort_values("timestamp", kind="stable")
        expected_times = pd.date_range(
            start=bucket, periods=expected_per_target, freq=source_step
        )
        observed_times = pd.DatetimeIndex(group["timestamp"])
        missing_times = expected_times.difference(observed_times)
        complete = len(group) == expected_per_target and observed_times.equals(expected_times)
        if not complete:
            incomplete_buckets += 1
            missing_count = int(len(missing_times))
            missing_slots_in_partial_buckets += missing_count
            if len(incomplete_examples) < max_gap_examples:
                incomplete_examples.append({
                    "target_bucket_start": bucket.isoformat(),
                    "observed_source_rows": int(len(group)),
                    "expected_source_rows": expected_per_target,
                    "missing_source_slots": missing_count,
                    "missing_slot_examples": [value.isoformat() for value in missing_times[:5]],
                    "reason": "incomplete_source_coverage",
                })
            continue

        output_rows.append({
            "timestamp": bucket,
            "open": float(group["open"].iloc[0]),
            "high": float(group["high"].max()),
            "low": float(group["low"].min()),
            "close": float(group["close"].iloc[-1]),
        })

    first_bucket = frame["target_bucket"].min()
    last_bucket = frame["target_bucket"].max()
    span_buckets = int((last_bucket - first_bucket).total_seconds() // target_interval) + 1
    observed_buckets = int(frame["target_bucket"].nunique())
    entirely_missing_buckets = max(span_buckets - observed_buckets, 0)
    complete_count = len(output_rows)
    result = pd.DataFrame(output_rows, columns=["timestamp", "open", "high", "low", "close"])
    result["timestamp"] = pd.to_datetime(result["timestamp"], utc=True)

    report: dict[str, Any] = {
        "mode": "quality_aware_ohlc_resampling",
        "source_interval_seconds": source_interval,
        "target_interval_seconds": target_interval,
        "source_timestamp_label": "open",
        "output_timestamp_label": "open",
        "expected_source_rows_per_target_bar": expected_per_target,
        "input_rows": int(len(frame)),
        "observed_target_buckets": observed_buckets,
        "expected_target_buckets_in_time_span": span_buckets,
        "complete_target_bars_written": complete_count,
        "incomplete_target_buckets_excluded": incomplete_buckets,
        "entirely_missing_target_buckets_in_span": entirely_missing_buckets,
        "missing_source_slots_inside_incomplete_buckets": missing_slots_in_partial_buckets,
        "complete_bar_coverage_ratio_over_time_span": (
            complete_count / span_buckets if span_buckets else 0.0
        ),
        "start_timestamp": first_bucket.isoformat(),
        "end_timestamp": last_bucket.isoformat(),
        "incomplete_examples": incomplete_examples,
        "notes": [
            "Only target intervals containing every expected source candle are emitted.",
            "No OHLC prices are interpolated or forward-filled.",
            "Missing full target intervals are counted separately from partial intervals.",
            "A gap-aware signal/backtest pipeline must still be used for the resulting sparse timeline.",
            "Five-second bars cannot reconstruct exact three-second entries or settlements.",
            "Structural completeness does not establish provider authenticity, OTC parity, or profitability.",
        ],
        "orders_submitted": 0,
        "profitability_claim": False,
    }
    return result, report


def _atomic_csv(frame: pd.DataFrame, destination: Path, *, overwrite: bool) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and not overwrite:
        raise FileExistsError(f"output already exists; refusing to overwrite: {destination}")
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="", dir=destination.parent,
            prefix=f".{destination.name}.", suffix=".tmp", delete=False,
        ) as temporary:
            temporary_name = temporary.name
            frame.to_csv(temporary, index=False)
            temporary.flush()
            os.fsync(temporary.fileno())
        if destination.exists() and not overwrite:
            raise FileExistsError(f"output already exists; refusing to overwrite: {destination}")
        os.replace(temporary_name, destination)
        temporary_name = None
    finally:
        if temporary_name and os.path.exists(temporary_name):
            os.unlink(temporary_name)


def _atomic_json(payload: dict[str, Any], destination: Path, *, overwrite: bool) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and not overwrite:
        raise FileExistsError(f"output already exists; refusing to overwrite: {destination}")
    serialized = json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=destination.parent,
            prefix=f".{destination.name}.", suffix=".tmp", delete=False,
        ) as temporary:
            temporary_name = temporary.name
            temporary.write(serialized)
            temporary.flush()
            os.fsync(temporary.fileno())
        if destination.exists() and not overwrite:
            raise FileExistsError(f"output already exists; refusing to overwrite: {destination}")
        os.replace(temporary_name, destination)
        temporary_name = None
    finally:
        if temporary_name and os.path.exists(temporary_name):
            os.unlink(temporary_name)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bpr02-resample-candles",
        description="Aggregate source OHLC candles without filling missing bars or executing trades.",
    )
    parser.add_argument("csv", help="Source OHLC CSV")
    parser.add_argument("--source-interval-seconds", type=int, default=5)
    parser.add_argument("--target-interval-seconds", type=int, default=60)
    parser.add_argument("--output", default="otc_candles_m1.csv")
    parser.add_argument("--report-output", default="bpr02_resample_report.json")
    parser.add_argument("--manifest-output", default="bpr02_resample_manifest.json")
    parser.add_argument("--max-gap-examples", type=int, default=20)
    parser.add_argument("--overwrite", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    source = Path(args.csv)
    output = Path(args.output)
    report_path = Path(args.report_output)
    manifest_path = Path(args.manifest_output)
    paths = [source.resolve(), output.resolve(), report_path.resolve(), manifest_path.resolve()]
    if len(set(paths)) != len(paths):
        print("ERROR: source, CSV output, report, and manifest paths must all differ", file=sys.stderr)
        return 2
    if not args.overwrite:
        existing = [str(path) for path in (output, report_path, manifest_path) if path.exists()]
        if existing:
            print(f"ERROR: output already exists; use --overwrite explicitly: {existing}", file=sys.stderr)
            return 2

    try:
        candles = load_ohlc_csv(source)
        resampled, report = resample_ohlc_to_interval(
            candles,
            source_interval_seconds=args.source_interval_seconds,
            target_interval_seconds=args.target_interval_seconds,
            max_gap_examples=args.max_gap_examples,
        )
        _atomic_csv(resampled, output, overwrite=args.overwrite)
        report["source_file"] = str(source)
        report["source_sha256"] = sha256_file(source)
        report["output_file"] = str(output)
        report["output_sha256"] = sha256_file(output)
        report["output_rows"] = int(len(resampled))
        _atomic_json(report, report_path, overwrite=args.overwrite)

        manifest = build_run_manifest(
            source_path=source,
            mode="quality_aware_ohlc_resampling",
            parameters={
                "source_interval_seconds": args.source_interval_seconds,
                "target_interval_seconds": args.target_interval_seconds,
                "timestamp_label": "open",
                "only_complete_target_bars": True,
            },
            outputs={"resampled_ohlc": output, "quality_report": report_path},
            rows=int(len(resampled)),
            start_timestamp=report["start_timestamp"] if not resampled.empty else None,
            end_timestamp=report["end_timestamp"] if not resampled.empty else None,
            summary={
                "complete_target_bars_written": report["complete_target_bars_written"],
                "incomplete_target_buckets_excluded": report["incomplete_target_buckets_excluded"],
                "entirely_missing_target_buckets_in_span": report["entirely_missing_target_buckets_in_span"],
                "complete_bar_coverage_ratio_over_time_span": report["complete_bar_coverage_ratio_over_time_span"],
            },
            notes=(
                "Only complete target intervals were emitted; no prices were filled or fabricated.",
                "This is a data-preparation step, not a strategy result or evidence of profitability.",
            ),
        )
        write_run_manifest(manifest_path, manifest, source_path=source)
        print(json.dumps({
            "mode": report["mode"],
            "output_file": str(output),
            "report_file": str(report_path),
            "manifest_file": str(manifest_path),
            **report,
            "orders_submitted": 0,
        }, indent=2, ensure_ascii=False, allow_nan=False))
        return 0
    except (FileExistsError, FileNotFoundError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

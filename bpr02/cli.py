"""Command-line entry point for reproducible CSV research."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .audit import audit_csv_file
from .backtest import backtest_coverage_diagnostics, backtest_fixed_expiry, summarize_results
from .data import load_ohlc_csv
from .provenance import build_run_manifest, write_run_manifest
from .sensitivity import run_sensitivity_analysis
from .signals import CALL, PUT, sweep_reclaim_signals
from .walkforward import run_walk_forward


def _parse_int_list(value: str) -> tuple[int, ...]:
    try:
        parsed = tuple(int(part.strip()) for part in value.split(",") if part.strip())
    except ValueError as exc:
        raise argparse.ArgumentTypeError("provide comma-separated integers") from exc
    if not parsed:
        raise argparse.ArgumentTypeError("provide at least one integer")
    return parsed


def _validate_manifest_path(args: argparse.Namespace) -> None:
    """Prevent results or manifests from overwriting input data or each other."""
    result_paths: list[Path] = []
    if args.audit_data:
        result_paths.append(Path(args.audit_output))
    elif args.sensitivity:
        result_paths.append(Path(args.sensitivity_output))
    else:
        result_paths.append(Path(args.output))
        if args.walk_forward:
            result_paths.append(Path(args.fold_summary))

    source = Path(args.csv).resolve()
    if source in {path.resolve() for path in result_paths}:
        raise ValueError("run output paths must not overwrite the source CSV")
    target = Path(args.manifest_output).resolve()
    protected = [source, *(path.resolve() for path in result_paths)]
    if target in set(protected):
        raise ValueError("--manifest-output must differ from the input and all run output paths")


def _iso_timestamp(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat") and callable(value.isoformat):
        return str(value.isoformat())
    return str(value)


def _save_manifest(
    *,
    args: argparse.Namespace,
    mode: str,
    parameters: dict[str, Any],
    outputs: dict[str, str],
    rows: int | None,
    start_timestamp: str | None,
    end_timestamp: str | None,
    summary: dict[str, Any],
    notes: tuple[str, ...] = (),
) -> str:
    manifest = build_run_manifest(
        source_path=args.csv,
        mode=mode,
        parameters=parameters,
        outputs=outputs,
        rows=rows,
        start_timestamp=start_timestamp,
        end_timestamp=end_timestamp,
        summary=summary,
        notes=notes,
    )
    destination = Path(args.manifest_output)
    write_run_manifest(destination, manifest, source_path=args.csv)
    return str(destination)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bpr02",
        description="Research-only OHLC fixed-expiry backtest; does not place trades.",
    )
    parser.add_argument("csv", help="Path to historical OHLC CSV")
    parser.add_argument("--output", default="bpr02_results.csv", help="Path for trade results CSV")
    parser.add_argument(
        "--manifest-output",
        default="bpr02_run_manifest.json",
        help="Path for reproducibility manifest JSON (SHA-256 data fingerprint, parameters and outputs)",
    )
    parser.add_argument("--lookback", type=int, default=12, help="Prior candles for sweep/reclaim (>=2)")
    parser.add_argument("--expiry-bars", type=int, default=1, help="Candles from entry to expiry (>=1)")
    parser.add_argument("--payout", type=float, default=0.80, help="Net win payout per unit stake (0..1)")
    parser.add_argument("--stake", type=float, default=1.0, help="Fixed unit stake (>0)")
    parser.add_argument("--tie-policy", choices=("push", "loss"), default="push")

    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--walk-forward", action="store_true", help="Evaluate successive future-only test windows")
    modes.add_argument("--sensitivity", action="store_true", help="Compare lookback/expiry combinations on chronological train and test segments")
    modes.add_argument("--audit-data", action="store_true", help="Audit timestamp intervals and candle quality, then exit")

    parser.add_argument("--initial-train-size", type=int, help="Initial training rows required with --walk-forward")
    parser.add_argument("--test-size", type=int, help="Rows per test window required with --walk-forward")
    parser.add_argument("--step-size", type=int, help="Rows to advance between test windows (defaults to test size)")
    parser.add_argument("--fold-summary", default="bpr02_fold_metrics.csv", help="Output CSV for walk-forward fold metrics")
    parser.add_argument("--lookbacks", type=_parse_int_list, default=(6, 12, 18), help="Comma-separated lookback grid for --sensitivity (e.g. 6,12,18)")
    parser.add_argument("--expiry-grid", type=_parse_int_list, default=(1, 2, 3), help="Comma-separated expiry grid for --sensitivity (e.g. 1,2,3)")
    parser.add_argument("--train-fraction", type=float, default=0.70, help="Chronological training fraction for --sensitivity (0..1)")
    parser.add_argument("--sensitivity-output", default="bpr02_sensitivity.csv", help="Output CSV for sensitivity table")
    parser.add_argument("--expected-interval-seconds", type=float, help="Expected candle spacing in seconds for audits, signals and backtests; inferred when omitted")
    parser.add_argument("--audit-output", default="bpr02_data_audit.json", help="Output JSON path for --audit-data")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        _validate_manifest_path(args)

        if args.audit_data:
            report = audit_csv_file(
                args.csv,
                expected_interval_seconds=args.expected_interval_seconds,
            )
            destination = Path(args.audit_output)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
            manifest_file = _save_manifest(
                args=args,
                mode="data_audit",
                parameters={"expected_interval_seconds": args.expected_interval_seconds},
                outputs={"audit_report": str(destination)},
                rows=int(report.get("rows", 0)),
                start_timestamp=report.get("start_timestamp"),
                end_timestamp=report.get("end_timestamp"),
                summary={
                    "structurally_valid": report.get("structurally_valid"),
                    "timestamp_parse_errors": report.get("timestamp_parse_errors"),
                    "duplicate_timestamps": report.get("duplicate_timestamps"),
                    "out_of_order_steps": report.get("out_of_order_steps"),
                    "invalid_ohlc_rows": report.get("invalid_ohlc_rows"),
                    "warnings": report.get("warnings", []),
                    "issues": report.get("issues", []),
                },
                notes=(
                    "Data-quality audit only; no strategy was run.",
                    "Timestamp gaps are estimates and do not verify parity with Pocket Option OTC.",
                ),
            )
            print(json.dumps({
                "mode": "data_audit",
                "audit_file": str(destination),
                "manifest_file": manifest_file,
                **report,
            }, indent=2, default=str))
            return 0 if report["structurally_valid"] else 2

        candles = load_ohlc_csv(args.csv)
        start_timestamp = _iso_timestamp(candles["timestamp"].iloc[0])
        end_timestamp = _iso_timestamp(candles["timestamp"].iloc[-1])

        if args.sensitivity:
            sensitivity = run_sensitivity_analysis(
                candles,
                lookbacks=args.lookbacks,
                expiry_bars=args.expiry_grid,
                expected_interval_seconds=args.expected_interval_seconds,
                train_fraction=args.train_fraction,
                payout=args.payout,
                stake=args.stake,
                tie_policy=args.tie_policy,
            )
            destination = Path(args.sensitivity_output)
            destination.parent.mkdir(parents=True, exist_ok=True)
            sensitivity.to_csv(destination, index=False)
            manifest_file = _save_manifest(
                args=args,
                mode="sensitivity_research_only",
                parameters={
                    "lookbacks": list(args.lookbacks),
                    "expiry_grid": list(args.expiry_grid),
                    "expected_interval_seconds": args.expected_interval_seconds,
                    "train_fraction": args.train_fraction,
                    "payout": args.payout,
                    "stake": args.stake,
                    "tie_policy": args.tie_policy,
                },
                outputs={"sensitivity_csv": str(destination)},
                rows=len(candles),
                start_timestamp=start_timestamp,
                end_timestamp=end_timestamp,
                summary={
                    "grid_rows": len(sensitivity),
                    "combinations_per_segment": len(args.lookbacks) * len(args.expiry_grid),
                    "segments": ["train", "test"],
                },
                notes=(
                    "Grid comparison is descriptive and does not select a winning configuration.",
                    "Repeatedly tuning on a holdout set can overfit it.",
                    "Historical results do not establish future profitability or OTC feed parity.",
                ),
            )
            print(json.dumps({
                "mode": "sensitivity_research_only",
                "source_rows": len(candles),
                "lookbacks": list(args.lookbacks),
                "expiry_grid": list(args.expiry_grid),
                "train_fraction": args.train_fraction,
                "combinations_per_segment": len(args.lookbacks) * len(args.expiry_grid),
                "segments": ["train", "test"],
                "sensitivity_file": str(destination),
                "manifest_file": manifest_file,
                "rows": sensitivity.to_dict(orient="records"),
                "warning": (
                    "Descriptive grid comparison only; do not choose a configuration "
                    "solely because it looks best in the tested sample. Historical results "
                    "do not establish future profitability or OTC feed parity."
                ),
            }, indent=2, default=str))
            return 0

        signals = sweep_reclaim_signals(
            candles, lookback=args.lookback,
            expected_interval_seconds=args.expected_interval_seconds,
        )
        if args.walk_forward:
            if args.initial_train_size is None or args.test_size is None:
                raise ValueError("--walk-forward requires --initial-train-size and --test-size")
            trades, fold_metrics = run_walk_forward(
                candles,
                initial_train_size=args.initial_train_size,
                test_size=args.test_size,
                step_size=args.step_size,
                lookback=args.lookback,
                expiry_bars=args.expiry_bars,
                expected_interval_seconds=args.expected_interval_seconds,
                payout=args.payout,
                stake=args.stake,
                tie_policy=args.tie_policy,
            )
            fold_summary_path = Path(args.fold_summary)
            fold_summary_path.parent.mkdir(parents=True, exist_ok=True)
            fold_metrics.to_csv(fold_summary_path, index=False)
            extra_summary = {
                "folds": len(fold_metrics),
                "fold_summary_file": str(fold_summary_path),
                "fold_metrics": fold_metrics.to_dict(orient="records"),
            }
            coverage_columns = (
                "candidate_signals",
                "eligible_candidate_signals",
                "excluded_due_to_timestamp_gaps",
                "excluded_due_to_missing_future_bars",
            )
            coverage_summary: dict[str, Any] = {
                key: int(fold_metrics[key].sum()) for key in coverage_columns
            }
            total_candidates = coverage_summary["candidate_signals"]
            coverage_summary["candidate_coverage_ratio"] = (
                coverage_summary["eligible_candidate_signals"] / total_candidates
                if total_candidates else None
            )
            coverage_summary["coverage_basis"] = "out_of_sample_test_folds_only"
            mode = "walk_forward_research_only"
            outputs = {"trade_results_csv": str(args.output), "fold_summary_csv": str(fold_summary_path)}
            parameters = {
                "lookback": args.lookback,
                "expiry_bars": args.expiry_bars,
                "expected_interval_seconds": args.expected_interval_seconds,
                "payout": args.payout,
                "stake": args.stake,
                "tie_policy": args.tie_policy,
                "initial_train_size": args.initial_train_size,
                "test_size": args.test_size,
                "step_size": args.step_size,
            }
        else:
            trades = backtest_fixed_expiry(
                candles,
                signals,
                expiry_bars=args.expiry_bars,
                expected_interval_seconds=args.expected_interval_seconds,
                payout=args.payout,
                stake=args.stake,
                tie_policy=args.tie_policy,
            )
            coverage_summary = backtest_coverage_diagnostics(
                candles,
                signals,
                expiry_bars=args.expiry_bars,
                expected_interval_seconds=args.expected_interval_seconds,
            )
            coverage_summary["coverage_basis"] = "full_input_series"
            extra_summary = {}
            mode = "research_only"
            outputs = {"trade_results_csv": str(args.output)}
            parameters = {
                "lookback": args.lookback,
                "expiry_bars": args.expiry_bars,
                "expected_interval_seconds": args.expected_interval_seconds,
                "payout": args.payout,
                "stake": args.stake,
                "tie_policy": args.tie_policy,
            }

        destination = Path(args.output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        trades.to_csv(destination, index=False)
        stats = summarize_results(trades)
        manifest_file = _save_manifest(
            args=args,
            mode=mode,
            parameters=parameters,
            outputs=outputs,
            rows=len(candles),
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp,
            summary={
                **coverage_summary,
                **stats,
                **({"folds": len(fold_metrics)} if args.walk_forward else {}),
            },
            notes=(
                "Historical simulation is not a guarantee of future results.",
                "The CSV feed and settlement assumptions may differ from Pocket Option OTC.",
            ),
        )
        print(json.dumps({
            "mode": mode,
            "source_rows": len(candles),
            **coverage_summary,
            "results_file": str(destination),
            "manifest_file": manifest_file,
            **stats,
            **extra_summary,
            "warning": (
                "Historical simulation is not a guarantee of future results. "
                "CSV feed and settlement may differ from Pocket Option OTC."
            ),
        }, indent=2, default=str))
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

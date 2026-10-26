"""Command-line entry point for reproducible CSV research."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .backtest import backtest_fixed_expiry, summarize_results
from .data import load_ohlc_csv
from .signals import CALL, PUT, sweep_reclaim_signals


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bpr02",
        description="Research-only OHLC fixed-expiry backtest; does not place trades.",
    )
    parser.add_argument("csv", help="Path to historical OHLC CSV")
    parser.add_argument("--output", default="bpr02_results.csv", help="Path for trade results CSV")
    parser.add_argument("--lookback", type=int, default=12, help="Prior candles for sweep/reclaim (>=2)")
    parser.add_argument("--expiry-bars", type=int, default=1, help="Candles from entry to expiry (>=1)")
    parser.add_argument("--payout", type=float, default=0.80, help="Net win payout per unit stake (0..1)")
    parser.add_argument("--stake", type=float, default=1.0, help="Fixed unit stake (>0)")
    parser.add_argument("--tie-policy", choices=("push", "loss"), default="push")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        candles = load_ohlc_csv(args.csv)
        signals = sweep_reclaim_signals(candles, lookback=args.lookback)
        trades = backtest_fixed_expiry(
            candles,
            signals,
            expiry_bars=args.expiry_bars,
            payout=args.payout,
            stake=args.stake,
            tie_policy=args.tie_policy,
        )
        destination = Path(args.output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        trades.to_csv(destination, index=False)
        print(json.dumps({
            "mode": "research_only",
            "source_rows": len(candles),
            "candidate_signals": int(signals["signal"].isin([CALL, PUT]).sum()),
            "results_file": str(destination),
            **summarize_results(trades),
            "warning": (
                "Historical simulation is not a guarantee of future results. "
                "CSV feed and settlement may differ from Pocket Option OTC."
            ),
        }, indent=2))
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

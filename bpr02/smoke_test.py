"""Run a deterministic end-to-end BPR-02 smoke test with synthetic candles.

No network calls, broker connection, real account data, or orders are used.
Results are software diagnostics only and say nothing about strategy profitability.
"""
from __future__ import annotations

import contextlib
import json
import math
import tempfile
from io import StringIO
from pathlib import Path

import pandas as pd

from .audit import audit_csv_file
from .backtest import backtest_fixed_expiry, summarize_results
from .data import load_ohlc_csv
from .execution import (
    DisabledExecutionAdapter,
    ExecutionNotEnabledError,
    OrderIntent,
)
from .indicators import atr, ema
from .replay import main as replay_main
from .review import record_review
from .quote_audit import audit_quote_series
from .otc_data import convert_tick_log_to_quotes
from .sensitivity import run_sensitivity_analysis
from .signal_bridge import SignalBridge, run_jsonl_bridge
from .signals import CALL, NO_TRADE, PUT, sweep_reclaim_signals
from .subminute import backtest_seconds_expiry, load_quote_csv, run_seconds_walk_forward
from .walkforward import run_walk_forward

LOOKBACK = 5
ROW_COUNT = 80


def _make_synthetic_csv(path: Path) -> None:
    """Write deterministic, deliberately synthetic OHLC candles for smoke testing."""
    rows: list[dict[str, object]] = []
    opens: list[float] = []
    highs: list[float] = []
    lows: list[float] = []
    closes: list[float] = []
    call_indices = {12, 32, 52, 72}
    put_indices = {22, 42, 62}

    for index in range(ROW_COUNT):
        base_close = 100.0 + math.sin(index / 3.0) * 0.5 + index * 0.002
        open_price = closes[-1] if closes else base_close - 0.03
        close_price = base_close
        high_price = max(open_price, close_price) + 0.12
        low_price = min(open_price, close_price) - 0.12

        if index >= LOOKBACK and index in call_indices:
            prior_low = min(lows[-LOOKBACK:])
            prior_high = max(highs[-LOOKBACK:])
            close_price = prior_low + 0.01
            low_price = prior_low - 0.01
            high_price = min(max(open_price, close_price) + 0.10, prior_high - 0.01)
        elif index >= LOOKBACK and index in put_indices:
            prior_low = min(lows[-LOOKBACK:])
            prior_high = max(highs[-LOOKBACK:])
            close_price = prior_high - 0.01
            high_price = prior_high + 0.01
            low_price = max(min(open_price, close_price) - 0.10, prior_low + 0.01)

        # Guard the synthetic fixture itself. This is not a market-price model.
        high_price = max(high_price, open_price, close_price)
        low_price = min(low_price, open_price, close_price)
        opens.append(float(open_price))
        highs.append(float(high_price))
        lows.append(float(low_price))
        closes.append(float(close_price))
        rows.append({
            "timestamp": pd.Timestamp("2025-01-01T00:00:00Z")
            + pd.Timedelta(minutes=index),
            "open": open_price,
            "high": high_price,
            "low": low_price,
            "close": close_price,
        })

    pd.DataFrame(rows).to_csv(path, index=False)


def run_smoke_test() -> dict[str, object]:
    """Exercise core research components and the no-execution boundary."""
    completed: list[str] = []

    with tempfile.TemporaryDirectory(prefix="bpr02-smoke-") as temporary:
        root = Path(temporary)
        source_csv = root / "synthetic_candles.csv"
        _make_synthetic_csv(source_csv)

        audit = audit_csv_file(source_csv, expected_interval_seconds=60)
        if not audit.get("structurally_valid"):
            raise RuntimeError(f"Synthetic fixture failed its data audit: {audit}")
        completed.append("csv_audit")

        candles = load_ohlc_csv(source_csv)
        if len(candles) != ROW_COUNT:
            raise RuntimeError(f"Expected {ROW_COUNT} candles; loaded {len(candles)}")
        completed.append("ohlc_import")

        ema_values = ema(candles["close"], period=5)
        atr_values = atr(candles, period=5)
        if ema_values.iloc[-1] != ema_values.iloc[-1] or atr_values.iloc[-1] != atr_values.iloc[-1]:
            raise RuntimeError("EMA or ATR did not produce a final value")
        completed.append("ema_atr")

        signals = sweep_reclaim_signals(candles, lookback=LOOKBACK)
        signal_count = int(signals["signal"].isin([CALL, PUT]).sum())
        if signal_count < 1:
            raise RuntimeError("Synthetic fixture did not exercise any candidate signal")
        completed.append("signal_generation")

        trades = backtest_fixed_expiry(
            candles, signals, expiry_bars=1, payout=0.80, stake=1.0, tie_policy="push"
        )
        metrics = summarize_results(trades)
        if metrics["trades"] != len(trades):
            raise RuntimeError("Backtest summary is inconsistent with trade rows")
        completed.append("fixed_expiry_backtest")

        walk_trades, fold_metrics = run_walk_forward(
            candles,
            initial_train_size=30,
            test_size=10,
            step_size=10,
            lookback=LOOKBACK,
            expiry_bars=1,
        )
        if fold_metrics.empty or not (fold_metrics["training_end"] < fold_metrics["test_start"]).all():
            raise RuntimeError("Walk-forward windows are empty or not chronological")
        completed.append("walk_forward")

        sensitivity = run_sensitivity_analysis(
            candles, lookbacks=(3, 5), expiry_bars=(1, 2), train_fraction=0.70
        )
        if len(sensitivity) != 8 or set(sensitivity["segment"]) != {"train", "test"}:
            raise RuntimeError("Sensitivity grid produced an unexpected result")
        completed.append("sensitivity_grid")

        # Exercise the second-expiry engine with synthetic one-second observations.
        # This verifies code paths only; it does not approximate OTC market truth.
        quote_periods = (ROW_COUNT + 2) * 60 * 4
        quote_times = pd.date_range(
            candles["timestamp"].iloc[0],
            periods=quote_periods,
            freq="250ms",
        )
        candle_closes = candles["close"].to_numpy(dtype=float)
        quote_prices: list[float] = []
        for quote_time in quote_times:
            elapsed = int((quote_time - candles["timestamp"].iloc[0]).total_seconds())
            minute_index = min(elapsed // 60, len(candle_closes) - 1)
            fraction = (elapsed % 60) / 60.0
            next_index = min(minute_index + 1, len(candle_closes) - 1)
            price = candle_closes[minute_index] + (
                candle_closes[next_index] - candle_closes[minute_index]
            ) * fraction
            quote_prices.append(float(price))
        synthetic_quotes = pd.DataFrame({
            "timestamp": quote_times,
            "price": quote_prices,
        })
        quote_audit = audit_quote_series(
            synthetic_quotes,
            expiry_seconds=3,
            target_resolution_fraction=0.25,
        )
        if quote_audit["temporal_resolution_assessment"] != "meets_configured_resolution_screen":
            raise RuntimeError("Synthetic quote fixture did not exercise the configured resolution screen")
        completed.append("quote_temporal_quality_audit")

        # Exercise the raw tick-log -> UTC-second CSV path without a network call.
        # The provider's documented OTC clock offset is simulated and remains configurable.
        raw_tick_log = root / "synthetic_otc_ticks.jsonl"
        raw_records: list[str] = []
        for tick_time, tick_price in zip(quote_times[::4], quote_prices[::4]):
            source_epoch = int(tick_time.timestamp()) + 7200
            raw_records.append(json.dumps({
                "schema_version": 1,
                "source": "otcharts",
                "venue": "otc",
                "event": "tick",
                "source_time_epoch_seconds": source_epoch,
                "payload": {
                    "symbol": "EURUSD_otc",
                    "time": source_epoch,
                    "price": tick_price,
                },
            }))
        raw_tick_log.write_text("\\n".join(raw_records) + "\\n", encoding="utf-8")
        converted_quotes_path = root / "converted_otc_quotes.csv"
        conversion = convert_tick_log_to_quotes(
            raw_log_path=raw_tick_log,
            output_path=converted_quotes_path,
            symbol="EURUSD_otc",
            source_clock_ahead_seconds=7200,
        )
        converted_quotes = load_quote_csv(converted_quotes_path)
        if conversion["unique_second_quotes"] != len(converted_quotes):
            raise RuntimeError("OTC tick conversion count does not match the quote CSV")
        completed.append("otc_tick_log_conversion")

        converted_audit = audit_quote_series(
            converted_quotes,
            expiry_seconds=3,
            target_resolution_fraction=0.50,
        )
        if converted_audit["temporal_resolution_assessment"] != "meets_configured_resolution_screen":
            raise RuntimeError("Converted quote fixture failed its configured temporal-resolution screen")
        completed.append("converted_quote_resolution_audit")

        seconds_trades, seconds_diagnostics = backtest_seconds_expiry(
            candles,
            signals,
            converted_quotes,
            bar_seconds=60,
            bar_timestamp_label="open",
            expiry_seconds=3,
            max_quote_delay_seconds=0,
            payout=0.80,
            stake=1.0,
        )
        if seconds_diagnostics["expiry_seconds"] != 3.0:
            raise RuntimeError("Seconds-expiry simulation did not preserve the requested expiry")
        if seconds_diagnostics["modeled_trades"] + seconds_diagnostics["unmodeled_signals"] != seconds_diagnostics["candidate_signals"]:
            raise RuntimeError("Seconds-expiry diagnostics failed to account for candidate signals")
        completed.append("seconds_expiry_backtest")

        seconds_walk_trades, seconds_fold_metrics, seconds_wf_diagnostics = run_seconds_walk_forward(
            candles,
            signals,
            converted_quotes,
            initial_train_size=30,
            test_size=10,
            step_size=10,
            bar_seconds=60,
            bar_timestamp_label="open",
            expiry_seconds=3,
            max_quote_delay_seconds=0,
            payout=0.80,
            stake=1.0,
        )
        if seconds_fold_metrics.empty:
            raise RuntimeError("Seconds-expiry walk-forward evaluation returned no folds")
        if not (seconds_fold_metrics["training_end"] < seconds_fold_metrics["test_start"]).all():
            raise RuntimeError("Seconds-expiry walk-forward contains non-future test windows")
        if seconds_wf_diagnostics["modeled_trades"] + seconds_wf_diagnostics["unmodeled_signals"] != seconds_wf_diagnostics["candidate_signals"]:
            raise RuntimeError("Seconds walk-forward diagnostics failed to account for candidate signals")
        completed.append("seconds_expiry_walk_forward")

        # Exercise the same CSV -> JSONL replay -> bridge -> review path used in E2E tests.
        replay_stdout = StringIO()
        with contextlib.redirect_stdout(replay_stdout):
            replay_code = replay_main([
                str(source_csv),
                "--symbol", "SYNTHETIC_DEMO",
                "--timeframe", "M1",
                "--source", "synthetic_csv_smoke_test",
            ])
        if replay_code != 0:
            raise RuntimeError(f"Historical replay failed with exit code {replay_code}")
        completed.append("historical_replay")

        event_path = root / "events.jsonl"
        bridge_output = StringIO()
        bridge = SignalBridge(
            lookback=LOOKBACK,
            symbol="SYNTHETIC_DEMO",
            timeframe="M1",
            source="synthetic_csv_smoke_test",
        )
        processed = run_jsonl_bridge(
            StringIO(replay_stdout.getvalue()),
            bridge_output,
            events_path=event_path,
            bridge=bridge,
        )
        event_lines = event_path.read_text(encoding="utf-8").splitlines()
        events = [json.loads(line) for line in event_lines]
        if processed != ROW_COUNT or len(events) != ROW_COUNT:
            raise RuntimeError("Signal bridge did not process every replayed candle")
        if any(event.get("order_sent") is not False for event in events):
            raise RuntimeError("Research event unexpectedly indicates order execution")
        completed.append("signal_bridge_and_event_log")

        selected = next(
            (event for event in events if event.get("signal") in {CALL, PUT}),
            None,
        )
        if selected is None:
            raise RuntimeError("No directional research event available for manual-review test")
        review_path = root / "reviews.jsonl"
        review = record_review(
            events_path=event_path,
            event_id=str(selected["event_id"]),
            decision="review",
            journal_path=review_path,
            note="Automated smoke test; manual review only",
        )
        if review.get("execution_performed") is not False or not review_path.is_file():
            raise RuntimeError("Manual-review journal did not preserve research-only mode")
        completed.append("manual_review_journal")

        intent = OrderIntent(
            event_id="smoke-event",
            approval_reference="smoke-test-only",
            symbol="SYNTHETIC_DEMO",
            timeframe="M1",
            source="synthetic_csv_smoke_test",
            signal_timestamp=str(selected["timestamp"]),
            side=str(selected["signal"]),
            stake=1.0,
            expiry_seconds=60,
        )
        try:
            DisabledExecutionAdapter().submit(intent)
        except ExecutionNotEnabledError:
            completed.append("execution_fail_closed")
        else:
            raise RuntimeError("Disabled execution adapter did not reject the request")

        return {
            "status": "passed",
            "mode": "synthetic_research_smoke_test",
            "checks_passed": completed,
            "check_count": len(completed),
            "synthetic_candles": len(candles),
            "candidate_signals": signal_count,
            "simulated_trades": len(trades),
            "walk_forward_folds": len(fold_metrics),
            "walk_forward_simulated_trades": len(walk_trades),
            "sensitivity_rows": len(sensitivity),
            "high_resolution_quote_observations": quote_audit["observations"],
            "high_resolution_quote_gap_p95_seconds": quote_audit["p95_gap_seconds"],
            "converted_quote_observations": converted_audit["observations"],
            "converted_quote_gap_p95_seconds": converted_audit["p95_gap_seconds"],
            "quote_temporal_resolution_assessment": quote_audit["temporal_resolution_assessment"],
            "otc_tick_rows_converted": conversion["tick_rows_for_symbol"],
            "otc_duplicate_second_ticks_collapsed": conversion["same_second_ticks_collapsed"],
            "seconds_expiry_candidates": seconds_diagnostics["candidate_signals"],
            "seconds_expiry_modeled_trades": len(seconds_trades),
            "seconds_expiry_unmodeled_signals": seconds_diagnostics["unmodeled_signals"],
            "seconds_walk_forward_folds": len(seconds_fold_metrics),
            "seconds_walk_forward_candidates": seconds_wf_diagnostics["candidate_signals"],
            "seconds_walk_forward_modeled_trades": len(seconds_walk_trades),
            "replay_events": len(events),
            "orders_submitted": 0,
            "execution_adapter": "disabled",
            "profitability_claim": False,
            "note": (
                "Synthetic software smoke test only. These generated candles are not market data "
                "and the results say nothing about strategy profitability or Pocket Option OTC parity."
            ),
        }


def main() -> int:
    try:
        print(json.dumps(run_smoke_test(), indent=2, ensure_ascii=False))
    except (OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, indent=2), flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

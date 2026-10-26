# BPR-02 — research-only fixed-time backtesting

BPR-02 is a Python research scaffold for testing directional CALL/PUT hypotheses on historical OHLC candles. It does **not** connect to Pocket Option, place orders, request credentials, or guarantee profitability.

## Current experimental pipeline

1. Import historical CSV candles and validate timestamps and OHLC ranges.
2. Calculate EMA and a simple rolling ATR indicator.
3. Generate an explicitly experimental sweep-and-reclaim candidate signal.
4. Simulate fixed-time settlement using the next candle's open for entry.
5. Export trade-level outcomes and simple descriptive metrics.

The sweep-and-reclaim rule is only a starting hypothesis; it has not been established as profitable. Never interpret candidate signals as personalized investment advice.

## Requirements

- Python 3.11 or newer
- pandas 2.1 or newer, below 4.0
- pytest for the test suite

Install from the repository root:

```bash
python -m pip install -e ".[test]"
python -m pytest
```

## Input CSV

The CSV must contain a timestamp column (one of `timestamp`, `datetime`, `date_time`, `date` or `time`) and numeric `open`, `high`, `low`, `close` columns. Column names are case-insensitive. Timestamps are parsed as UTC; values without a timezone are interpreted as UTC. Extra columns are preserved. Rows are sorted chronologically, while duplicate timestamps and invalid OHLC ranges are rejected.

Example:

```csv
timestamp,open,high,low,close
2025-01-01T00:00:00Z,1.0400,1.0410,1.0390,1.0405
2025-01-01T00:01:00Z,1.0405,1.0415,1.0400,1.0410
```

Use a CSV from a source you are permitted to use. Do not commit private account data, API keys, or sensitive historical datasets to this public repository.

## Run a backtest

```bash
python -m bpr02 path/to/candles.csv --output results.csv --lookback 12 --expiry-bars 1 --payout 0.80 --stake 1
```

To run expanding walk-forward evaluation and export a per-fold metrics CSV:

```bash
python -m bpr02 path/to/candles.csv --walk-forward --initial-train-size 5000 --test-size 1000 --step-size 1000 --lookback 12 --output trades.csv --fold-summary folds.csv
```

The walk-forward mode requires both `--initial-train-size` and `--test-size`. Training windows expand chronologically; test windows stay in the future and do not overlap by default. Choose window sizes based on the dataset, and do not repeatedly tune the rule against the final evaluation window.

Options:
- `--lookback`: prior candles in the sweep/reclaim reference range (minimum 2).
- `--expiry-bars`: candles from entry to settlement (minimum 1). With value 1, entry is at the next candle open and settlement is that candle's close.
- `--payout`: net profit multiplier for a winning trade; 0.80 means +0.80 units on a one-unit stake.
- `--stake`: fixed hypothetical stake, in arbitrary units.
- `--tie-policy push|loss`: whether equal entry and expiry prices produce zero P/L or a loss.

The command writes a trade-level CSV and prints a JSON summary, including win rate excluding ties, a Wilson 95% interval for the observed win proportion, theoretical break-even win rate for the modeled payout, profit factor, expectancy and maximum drawdown in stake units. The interval reflects sampling uncertainty in the win proportion only; it is not proof of profitability or a forecast. These are historical descriptive metrics, not forecasts. No compounding, martingale, commissions, latency, variable payout, quote-provider differences or platform-specific settlement rules are modeled.

## Chronological evaluation helpers

Use `chronological_split(frame, train_fraction=0.70)` to reserve the later portion of data as a test set without shuffling. Use `expanding_window_splits(n_rows, initial_train_size=..., test_size=...)` to build multiple expanding training windows and later, non-overlapping test blocks. These helpers create partitions only; they do not fit strategies or automatically prevent a caller from using test results to tune parameters. Keep model selection and final evaluation data separate. The `run_walk_forward(...)` helper evaluates the baseline on each future test block and returns both trade-level results and a per-fold metrics table; it does not tune the rule on the test blocks.

## Important limitations

- Historical simulation is not a guarantee of future results.
- This candle-based model cannot establish that an OTC feed matches the source CSV. OTC prices, expiry timestamps, tie rules and payout can differ from the assumptions.
- The next-open entry is a modeling convention, not a promise of executable fills.
- Out-of-sample testing, chronological splits, spread/feed checks and sensitivity analysis are still needed before drawing any conclusions.
- BPR-02 is research-only; it contains no live execution module.

## Project layout

- `bpr02/data.py`: CSV import and validation.
- `bpr02/indicators.py`: EMA and simple rolling ATR.
- `bpr02/signals.py`: experimental sweep/reclaim candidates and reasons.
- `bpr02/backtest.py`: fixed-expiry simulation and metrics.
- `bpr02/evaluation.py`: chronological train/test and expanding walk-forward splits.
- `bpr02/walkforward.py`: per-fold fixed-expiry simulations and fold metrics.
- `bpr02/cli.py`: command-line interface.
- `tests/`: regression tests.

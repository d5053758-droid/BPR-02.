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

To compare lookback and expiry combinations across earlier training data and a later holdout segment:

```bash
python -m bpr02 path/to/candles.csv --sensitivity --lookbacks 6,12,18 --expiry-grid 1,2,3 --train-fraction 0.70 --sensitivity-output sensitivity.csv
```

To inspect the CSV before strategy testing and report missing-interval estimates:

```bash
python -m bpr02 path/to/candles.csv --audit-data --expected-interval-seconds 60 --audit-output data_audit.json
```

When no expected interval is supplied, BPR-02 infers the most common positive observed interval (shorter interval wins a tie). Large gaps are estimates, not automatic proof of missing data; sessions and feed behavior can also create gaps. The audit reads the raw CSV in its original row order so it can report duplicate timestamps, out-of-order rows, missing fields and invalid OHLC ranges. The normal backtest importer remains strict and rejects malformed/duplicate input.

This writes one row per lookback/expiry combination and segment (train/test). The grid is a diagnostic, not an automatic optimizer: repeated choices based on the holdout set can overfit it.

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

## End-to-end test with a historical CSV replay

BPR-02 provides a deterministic replay adapter so the signal pipeline can be tested without a live source. It is explicitly **not a real-time connection**.

In one terminal, replay candles from a CSV and pipe them into the JSONL signal bridge:

```bash
bpr02-replay data.csv --symbol EURUSD --timeframe M1 --interval-seconds 1 | bpr02-stream --lookback 12 --symbol EURUSD --timeframe M1 --events-out events.jsonl
```

Omit `--interval-seconds 1` to replay as fast as possible. Each replayed line is marked `source_mode: historical_replay_not_live`. The bridge writes its event log; it does not submit orders.

## Connecting a data source

The adapter contract and preconditions for any future platform connector are documented in [docs/FEED_ADAPTER_CONTRACT.md](docs/FEED_ADAPTER_CONTRACT.md). The current feasibility investigation is tracked in [docs/POCKET_OPTION_CONNECTOR_RESEARCH.md](docs/POCKET_OPTION_CONNECTOR_RESEARCH.md). A provider-specific connection has not been implemented because an authorized, documented Pocket Option data interface has not been established in this project.

## Signal bridge (authorized feed input; no execution)

The bridge does not connect to Pocket Option or any network endpoint. It reads one completed candle per JSON Lines record from standard input, generates a deterministic research alert, writes each event to an append-only log, and echoes events to standard output. A compatible adapter for a feed you are authorized to use can pipe records into it.

Example record (one JSON object per line):

```json
{"timestamp":"2025-01-01T00:00:00Z","open":1.04,"high":1.041,"low":1.039,"close":1.0405,"closed":true,"symbol":"EURUSD","timeframe":"M1","source":"authorized_demo_feed"}
```

Install the project and start the bridge:

```bash
python -m pip install -e .
bpr02-stream --lookback 12 --expiry-bars 1 --symbol EURUSD --timeframe M1 --source authorized_demo_feed --events-out events.jsonl
```

Paste or pipe complete-candle JSON Lines into standard input (Ctrl+D on Linux/macOS or Ctrl+Z then Enter on Windows ends input). For warm-up, an optional valid history CSV can be supplied with `--history-csv`. The first candles produce `NO TRADE` until enough history exists. Repeated, out-of-order or unfinished candles are rejected. A bridge instance also rejects changes to symbol, timeframe or source midstream, preventing accidental mixing of instruments or feeds. Each event includes `event_id`, timestamp, signal, reason, expiry assumption, and `order_sent: false`.

To record a manual review (this **does not** submit an order):

```bash
bpr02-review --events events.jsonl --event-id EVENT_ID_FROM_ALERT --decision review --journal reviews.jsonl --note "Compared with platform chart"
```

Choose `review` to mark an alert for manual inspection or `ignore` to dismiss it. Neither decision means a trade has been placed. This bridge is ready for an authorized feed adapter, but no Pocket Option adapter or connection is included until the platform's permitted interface and data format are verified.

## Important limitations

- Historical simulation is not a guarantee of future results.
- This candle-based model cannot establish that an OTC feed matches the source CSV. OTC prices, expiry timestamps, tie rules and payout can differ from the assumptions.
- The next-open entry is a modeling convention, not a promise of executable fills.
- Out-of-sample testing, chronological splits, spread/feed checks and sensitivity analysis are still needed before drawing any conclusions.
- BPR-02 is research-only. `bpr02/execution.py` defines a future adapter boundary only; the included `DisabledExecutionAdapter` always rejects requests. No code path submits orders, and this interface is not connected to the signal bridge or CLI. See `docs/FUTURE_EXECUTION_BOUNDARY.md`.

## Project layout

- `bpr02/data.py`: CSV import and validation.
- `bpr02/indicators.py`: EMA and simple rolling ATR.
- `bpr02/signals.py`: experimental sweep/reclaim candidates and reasons.
- `bpr02/backtest.py`: fixed-expiry simulation and metrics.
- `bpr02/evaluation.py`: chronological train/test and expanding walk-forward splits.
- `bpr02/walkforward.py`: per-fold fixed-expiry simulations and fold metrics.
- `bpr02/signal_bridge.py`: closed-candle JSONL alert bridge and append-only event log.
- `bpr02/execution.py`: fail-closed interface for a possible future authorized adapter; no live adapter or order submission is implemented.
- `bpr02/review.py`: append-only manual signal review journal.
- `bpr02/replay.py`: deterministic historical CSV-to-JSONL test adapter (not live).
- `docs/FEED_ADAPTER_CONTRACT.md`: data-provider interface contract and connection requirements.
- `docs/FUTURE_EXECUTION_BOUNDARY.md`: prerequisites, approval boundary and safeguards for any future execution adapter.
- `bpr02/sensitivity.py`: chronological train/test sensitivity grid for lookback and expiry.
- `bpr02/audit.py`: timestamp-interval, gap and OHLC diagnostics.
- `bpr02/cli.py`: command-line interface.
- `tests/`: regression tests.

# BPR-02 — research-only fixed-time backtesting

BPR-02 is a Python research scaffold for testing directional CALL/PUT hypotheses on historical OHLC candles. It does **not** connect to a Pocket Option trading account or place orders. An optional, read-only third-party data adapter can read its own API key from a local environment variable; it never needs a Pocket Option login or session token. Nothing guarantees profitability.

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
bpr02-smoke
python -m pytest
```

## Reproducible software smoke test

After installation, run:

```bash
bpr02-smoke
```

The command generates temporary, deliberately synthetic OHLC candles and timestamped quotes and checks CSV import/audit, EMA/ATR, candidate generation, fixed-expiry simulation, candle-based walk-forward, sensitivity analysis, quote temporal-quality audit, a mocked OTC candle API fetch, raw tick-log conversion, seconds-expiry simulation, seconds-expiry walk-forward evaluation, historical replay, signal-event logging, manual review journaling, and the disabled execution boundary. It deletes its temporary files on exit and does not require a market-data file, network access, broker credentials, or a Pocket Option connection.

A successful result reports `status: passed`, the checks completed, and `orders_submitted: 0`. This is a software integration check only: the candles are synthetic, and passing the check does not demonstrate a profitable strategy or parity with any broker's OTC prices. GitHub Actions runs this command before the unit-test suite on Python 3.11 and 3.12.

## Run manifests and reproducibility

Each `bpr02` run writes a JSON manifest (default: `bpr02_run_manifest.json`; override it with `--manifest-output path/to/manifest.json`). It includes the source file's SHA-256 fingerprint and byte size, the normalized dataset row count and timestamp range, all strategy/evaluation parameters, output paths, and a compact result summary. The data-audit mode also writes a manifest, including when the audit finds structural problems.

Keep the input CSV and its manifest together when archiving research results. If the source file's hash changes, it is not the same input dataset. The manifest does not establish where prices came from, prove OTC parity, or validate profitability. The CLI also refuses manifest paths that would overwrite the input or run outputs.

## Input CSV

The CSV must contain a timestamp column (one of `timestamp`, `datetime`, `date_time`, `date` or `time`) and numeric `open`, `high`, `low`, `close` columns. Column names are case-insensitive. Timestamps are parsed as UTC; values without a timezone are interpreted as UTC. Extra columns are preserved. Rows are sorted chronologically, while duplicate timestamps and invalid OHLC ranges are rejected.

For timestamped files, the experimental signal detector resets its lookback after any interval that differs from the inferred most common candle spacing. The bar-based backtester also omits a simulated trade if the signal-to-settlement window crosses an observed timestamp gap. Inference cannot detect a dataset that has systematically omitted bars but remains regularly spaced; provide and verify the expected interval for the source before trusting a result.

Example:

```csv
timestamp,open,high,low,close
2025-01-01T00:00:00Z,1.0400,1.0410,1.0390,1.0405
2025-01-01T00:01:00Z,1.0405,1.0415,1.0400,1.0410
```

Use a CSV from a source you are permitted to use. Do not commit private account data, API keys, or sensitive historical datasets to this public repository.

## Convert 5-second OTC candles to complete M1 bars

After the one-request data fetch, you can prepare complete 1-minute bars without filling missing prices:

```bash
bpr02-resample-candles otc_candles_5s.csv \
  --source-interval-seconds 5 \
  --target-interval-seconds 60 \
  --output otc_candles_m1.csv \
  --report-output otc_resample_report.json \
  --manifest-output otc_resample_manifest.json
```

The adapter expects source timestamps to identify the **opening time** of each 5-second candle. It writes a 1-minute OHLC row only when all 12 expected source candles are present on the 5-second UTC grid. Partial minutes are excluded, and the JSON report separately counts partial intervals, estimated missing source slots and fully missing minutes. Nothing is interpolated or forward-filled. The report and manifest fingerprint the input/output data.

Review the report before testing any rule. If the output has time gaps, BPR-02 resets signal lookback at those gaps and will not score a bar-based trade whose signal-to-settlement window crosses one. This automatic continuity check infers the most common observed spacing; it cannot detect a systematic omission when the remaining timestamps are still regularly spaced, so compare the source's declared timeframe with the report. Pin the expected interval for the actual backtest as well:

```bash
python -m bpr02 otc_candles_m1.csv \
  --expected-interval-seconds 60 \
  --output otc_m1_bar_trades.csv \
  --manifest-output otc_m1_bar_manifest.json
```

The same `--expected-interval-seconds 60` option applies to ordinary bar backtests, walk-forward runs, sensitivity checks and data audits. Set it to the real source candle spacing; do not use a value merely because it yields more signals.

You can audit the resulting M1 file too:

```bash
python -m bpr02 otc_candles_m1.csv \
  --audit-data \
  --expected-interval-seconds 60 \
  --audit-output otc_candles_m1_audit.json \
  --manifest-output otc_candles_m1_audit_manifest.json
```

This conversion supports M1 pattern/indicator research. It does **not** recover the within-candle price path and is not suitable for reconstructing exact three-second entries or settlement. A clean report proves only structural completeness of the received file, not data-provider authenticity, Pocket Option OTC parity or profitability.

## Dataset readiness preflight

Before running any backtest, run the consolidated preflight on your candle CSV with the **actual timeframe explicitly specified**:

```bash
bpr02-readiness otc_candles_m1.csv \
  --expected-interval-seconds 60 \
  --max-missing-bars 0 \
  --output readiness.json
```

For second-expiry research, include the timestamped quote file so the report can check sampling resolution and whether the quotes extend through the last candle's modeled settlement horizon:

```bash
bpr02-readiness otc_candles_m1.csv \
  --quotes-csv otc_quotes.csv \
  --expected-interval-seconds 60 \
  --bar-timestamp-label open \
  --expiry-seconds 3 \
  --max-quote-delay-seconds 0.25 \
  --max-quote-p95-gap-seconds 0.75 \
  --max-quote-gap-seconds 0.75 \
  --output seconds_readiness.json
```

By default the preflight also requires enough rows for `lookback=12` and `expiry-bars=1`: at least 14 candle rows for bar-based expiry, or 13 when quote observations model second-based settlement. Override with `--lookback` and, for bar-based tests, `--expiry-bars` to match the planned experiment. This row-count gate does not guarantee candidate signals exist or that the sample is statistically sufficient.

The command writes a JSON report even when data-quality gates fail. Exit code **0** means the configured preflight gates passed; **3** means at least one gate failed; **2** means the inputs or parameters could not be processed. The checks include candle structure, explicit source cadence, missing-bar budget, quote p95 gap, and quote time-range coverage. The optional `--max-quote-gap-seconds` adds a separate hard limit for the **largest individual gap**, because a p95 metric can hide rare long gaps. Without this option, the report shows the observed maximum gap but explicitly marks the gate as unenforced. When no explicit p95 limit is supplied, the default p95 screen is expiry multiplied by `--target-resolution-fraction` (0.25 by default); this is a heuristic, not a universal standard. Increase `--max-missing-bars` only when you deliberately accept the estimated gaps and have reviewed the gap report.

For a bar-only CSV, omit `--quotes-csv`. The report then says whether the candles pass the bar-based preflight; it does not claim seconds-expiry readiness. A passing report is only a data-quality screen: it does not prove source identity, Pocket Option OTC parity, exact settlement reconstruction, strategy validity, or profitability. Synthetic CI fixtures check software behavior only.

## Quote-feed quality preflight

Before interpreting a seconds-expiry backtest, audit the timestamps and sampling gaps in the quote CSV:

```bash
bpr02-quote-audit quotes.csv --expiry-seconds 3 --target-resolution-fraction 0.25 --output quote_audit.json
```

The report records the source file's SHA-256 hash, observation count and time span, median/p95/maximum quote interval, and how often gaps exceed the configured threshold. The default target p95 interval is one quarter of the requested expiry; this is a **configurable screening heuristic**, not a universal standard. To enforce an explicit local threshold, add `--max-p95-gap-seconds 0.25`; the command saves the report and exits with status 3 if the p95 interval exceeds the threshold **or cannot be measured**. The JSON report records `quality_threshold_enforced`, `quality_threshold_passed`, and `all_quality_gates_passed`; without an explicit threshold, the first is `false` and the second is `null`, so an unconfigured gate is not confused with an enforced pass.

This audit checks temporal resolution only. It does not prove the quotes came from the intended provider, match Pocket Option OTC, include the platform's settlement price, or support a profitable strategy. For three-second research, a dataset that fails the chosen resolution threshold should not be treated as conclusive evidence about outcomes.

## First real-data run (one request)

From the repository root in Codespaces, install the project once and run the starter script:

```bash
python -m pip install -e ".[test]"
bash scripts/first_otc_data_run.sh
```

The script asks for the OTCharts key with hidden terminal input, sends exactly one historical candle request for `EURUSD_otc` at five-second resolution, saves a CSV and JSON acquisition sidecar, then runs the readiness preflight against those exact files. It refuses to overwrite existing output files; rename them or set different `BPR02_CANDLES_OUTPUT`, `BPR02_FETCH_REPORT_OUTPUT`, or `BPR02_READINESS_OUTPUT` paths. The API key is not embedded in commands or written to the reports, and the script unsets it after the request. Never paste the key into this chat, a source file, or a GitHub commit.

The result is a data-quality experiment, not a signal run. If readiness exits with code 3, open the JSON report and fix or deliberately review the failed gate before backtesting. A provider-reported closed status is historical metadata, not proof that the instrument is currently tradable. No orders are sent.

## Optional read-only OTC data capture

BPR-02 includes an optional adapter for the **third-party OTCharts market-data API**. It is not an official Pocket Option API, it cannot place orders, and it never needs a Pocket Option login/session. Read the provider's current [API documentation](https://otcharts.com/api) and [Pocket Option data page](https://otcharts.com/es/pocket-option-api) before use. Pocket Option's own [public offer](https://pocketoption.com/es/public-offer) says it reserves the right to review results for operations performed with unauthorized bot software, so this adapter stays read-only.

**Start with the no-streaming, one-request path.** OTCharts' current API reference says its free key includes a limited number of candle requests but no live stream. No plan purchase is needed just to test the data import; use the current access and quota shown by your provider account.

Enter the provider's own API key without echoing it to the terminal. Never paste it into chat, commit it to GitHub, or write it into a file:

```bash
read -r -s -p "OTCharts API key: " OTCHARTS_API_KEY
printf '\n'
export OTCHARTS_API_KEY
bpr02-otc-data candles --symbol EURUSD_otc --timeframe-seconds 5 --limit 1500 --output otc_candles_5s.csv
unset OTCHARTS_API_KEY
```

This performs **one read-only request** for the latest page of five-second OTC candles. The adapter validates the returned symbol, candle timestamps and OHLC ranges, adjusts the provider's documented two-hour OTC clock offset to UTC, and records a SHA-256 hash and coverage summary. When the response includes the provider's current `open` field, the result also preserves it as `instrument_status`; this is current status metadata, not a guarantee that the instrument will be open during a future session or a signal to trade. It refuses to overwrite an existing CSV unless `--overwrite` is explicitly provided. The number of candles actually returned may be lower than requested; inspect the JSON summary rather than assuming the requested row count was delivered. If the provider reports an interval in the response, the adapter now rejects a mismatch with the requested timeframe and reports timestamp-spacing irregularities/missing-bar estimates separately from the provider's own `gaps` metadata.

Save the acquisition report next to the CSV so provider status, declared interval, timestamp-gap diagnostics and the source hash remain available:

```bash
bpr02-otc-data candles --symbol EURUSD_otc --timeframe-seconds 5 \
  --limit 1500 \
  --output otc_candles_5s.csv \
  --report-output otc_candles_5s_report.json
```

The JSON sidecar contains the acquisition summary and does not store the API key. The report path must differ from the CSV path; existing files are preserved unless `--overwrite` is explicitly supplied. A sidecar improves reproducibility, but is not a signed or independently verified provider attestation.

Run the preflight against both files to verify that the report belongs to this exact CSV and that the provider-reported symbol, timeframe and gap metadata are consistent:

```bash
bpr02-readiness otc_candles_5s.csv \
  --provider-report otc_candles_5s_report.json \
  --expected-symbol EURUSD_otc \
  --expected-interval-seconds 5 \
  --max-missing-bars 0 \
  --lookback 12 \
  --output otc_candles_5s_readiness.json
```

When a provider report is supplied, readiness adds gates for report type, CSV SHA-256 match, instrument/venue, timeframe and provider gap metadata. The status field (`open`/`closed`) is reported but does not disqualify historical research by itself. These are consistency checks on local files, not cryptographic proof that the provider produced them.

Inspect the exported file in BPR-02:

```bash
python -m bpr02 otc_candles_5s.csv --audit-data --expected-interval-seconds 5 --audit-output otc_candles_5s_audit.json
```

### Limitation for the three-second strategy

Five-second OHLC candles are **not sufficient to reconstruct a three-second contract's exact entry and settlement price**. They are useful for verifying timestamps, OHLC quality, recent OTC movement and the research pipeline, but they do not validate the three-second hypothesis. Do not treat a simulated win rate from five-second bars as evidence of three-second profitability.

The optional `record` subcommand records raw server-sent tick events when the provider account actually has live-stream permission. The API reference currently lists no stream on the free tier, so the command can be refused without the relevant entitlement. Do not buy a plan solely to run an experiment; use the candle query first and decide later whether finer data are worth seeking. The recorder never writes its API key into the log and refuses to overwrite existing recordings by default:

```bash
read -r -s -p "OTCharts API key: " OTCHARTS_API_KEY
printf '\n'
export OTCHARTS_API_KEY
bpr02-otc-data record --symbols EURUSD_otc --duration-seconds 1800 --output otc_ticks_raw.jsonl
unset OTCHARTS_API_KEY
```

If you already have authorized streaming access, convert the raw tick log into unique UTC-second quotes and audit the coverage:

```bash
bpr02-otc-data convert otc_ticks_raw.jsonl --symbol EURUSD_otc --output otc_quotes.csv
bpr02-quote-audit otc_quotes.csv --expiry-seconds 3 --target-resolution-fraction 0.25 --output otc_quote_audit.json
```

OTCharts documents whole-second tick timestamps; repeated ticks in the same source second are collapsed to the last tick. Even a stream is therefore not guaranteed to reconstruct subsecond ordering, network latency or the exact broker settlement price. Its data come from a third party, not a supported Pocket Option execution interface.

## Research backtests for expiry measured in seconds

The fixed-expiry bar backtester settles at candle closes. For a three-second hypothesis based on M1 signals, use the separate `bpr02-seconds-backtest` command with two files:

- **Signal candles CSV:** `timestamp,open,high,low,close`. Each timestamp must include a timezone or UTC offset. Use `--bar-timestamp-label open` if the timestamp is the start of each M1 candle; use `close` if it is the time the fully formed candle closed.
- **Quote observations CSV:** `timestamp,price`. Every timestamp must include a timezone/UTC offset; rows must be unique, sorted chronologically, and the price must be finite and positive. Use a feed with adequate timestamp precision and sampling frequency for the expiry under study.

Example:

```bash
bpr02-seconds-backtest candles_m1.csv quotes.csv --lookback 12 --bar-seconds 60 --bar-timestamp-label open --expiry-seconds 3 --entry-delay-seconds 0.10 --max-quote-delay-seconds 0.25 --min-model-coverage 0.95 --min-modeled-trades 30 --max-quote-p95-gap-seconds 0.75 --payout 0.80 --stake 1 --quote-source-label "my_authorized_historical_feed" --output seconds_trades.csv --manifest-output seconds_manifest.json
```

The CLI uses `--min-model-coverage 0.95` and `--min-modeled-trades 30` by default. The coverage gate requires at least 95% of candidate signals to have both modeled entry and settlement quotes; a low ratio or zero candidate signals causes exit status 3. The modeled-sample gate separately requires 30 scored outcomes before the run is marked as passing its configured quality gates. Both gates save the trade CSV and manifest before returning status 3, and the manifest records their pass/fail states. The 30-trade floor is only a basic small-sample safeguard: it is not a universal statistical standard, does not establish independent observations, and does not prove profitability. Use a lower `--min-modeled-trades` value only for debugging or explicitly exploratory checks, and do not interpret unmodeled signals as losses or wins.

The seconds backtest also reports `quote_temporal_resolution_assessment`, `quote_gap_p95_seconds`, and the heuristic target `quote_target_p95_gap_seconds` (one quarter of expiry by default). The label is a sampling-resolution screen, not evidence that prices match the broker or that settlement is exact. To enforce a separate p95 interval limit, add `--max-quote-p95-gap-seconds 0.75` for a three-second expiry. This optional hard gate exits with status 3 and still saves the trade CSV and manifest when the p95 gap exceeds the limit or cannot be measured. In ordinary (non-walk-forward) mode, the gate uses the full quote input series. In `--walk-forward` mode, it uses the worst measurable per-fold p95 gap from out-of-sample test folds that contain candidate signals; if any such fold's p95 cannot be measured, the gate fails closed rather than borrowing dense timestamps from training or unrelated periods. The manifest and CLI output name the gate scope and the exact p95 value used. Without the option, resolution is reported but does not independently fail the run or emit a resolution-threshold failure message.

The model waits until the signal candle is complete, selects the first quote at or after the entry target, then selects the first quote at or after the requested expiry time. It records how late each selected quote is. If no quote exists or the selected quote exceeds the configured delay tolerance, the signal is marked as unmodeled rather than inventing an entry or settlement. Review `no_entry_quote`, `stale_entry_quote`, `no_settlement_quote`, and `stale_settlement_quote` before interpreting the results. A smaller delay tolerance rejects more observations; a larger tolerance makes the modeled settlement less precise.

For an expanding, chronological out-of-sample evaluation of the same fixed rule, add `--walk-forward --initial-train-size 5000 --test-size 1000`. Add `--step-size 1000` to make the non-overlapping test windows explicit and `--fold-summary seconds_folds.csv` to choose the per-fold metrics file. The command reports only directional signals from future test windows; the rule is not fitted or optimized within these folds. It records candidate signals for which an entry or settlement quote could not be modeled rather than inventing outcomes.

**Important limitation:** this is a timestamped-quote approximation, not a broker-certified reconstruction of three-second execution or settlement. The data must match the market and price convention being researched. Standard EUR/USD quotes do not establish parity with Pocket Option's EUR/USD OTC feed. No broker is contacted and no orders are placed. A successful test of the code is not evidence of strategy profitability.

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

The CLI summary and run manifest also report candidate data coverage: `candidate_signals`, `eligible_candidate_signals`, `excluded_due_to_timestamp_gaps`, `excluded_due_to_missing_future_bars`, and `candidate_coverage_ratio`. Coverage is calculated only across directional candidates, not no-signal candles. In walk-forward mode, the top-level counts cover the out-of-sample test folds only; the fold-summary CSV reports the same diagnostics per fold. These counts make data-related omissions visible and are not predictive-performance metrics.

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
- `bpr02/backtest.py`: fixed-expiry bar simulation and metrics.
- `bpr02/subminute.py`: second-expiry research using timestamped quote observations; exposed as `bpr02-seconds-backtest`.
- `bpr02/quote_audit.py`: preflight quote sampling-gap audit, exposed as `bpr02-quote-audit`.
- `bpr02/otc_data.py`: optional third-party read-only OTC candle fetch, tick recorder and converter, exposed as `bpr02-otc-data`; no broker orders or login.
- `bpr02/evaluation.py`: chronological train/test and expanding walk-forward splits.
- `bpr02/walkforward.py`: per-fold fixed-expiry simulations and fold metrics.
- `bpr02/signal_bridge.py`: closed-candle JSONL alert bridge and append-only event log.
- `bpr02/execution.py`: fail-closed interface for a possible future authorized adapter; no live adapter or order submission is implemented.
- `bpr02/review.py`: append-only manual signal review journal.
- `bpr02/replay.py`: deterministic historical CSV-to-JSONL test adapter (not live).
- `bpr02/smoke_test.py`: runnable synthetic end-to-end software check, exposed as `bpr02-smoke`.
- `docs/FEED_ADAPTER_CONTRACT.md`: data-provider interface contract and connection requirements.
- `docs/FUTURE_EXECUTION_BOUNDARY.md`: prerequisites, approval boundary and safeguards for any future execution adapter.
- `bpr02/sensitivity.py`: chronological train/test sensitivity grid for lookback and expiry.
- `bpr02/audit.py`: timestamp-interval, gap and OHLC diagnostics.
- `bpr02/cli.py`: command-line interface.
- `tests/`: regression tests.


## Local read-only API

BPR-02 also includes a local read-only HTTP API that serves **only real files already recorded on disk**. It does not create market prices and does not connect to a Pocket Option account.

Install and start it from the repository root:

```bash
python -m pip install -e ".[test]"
bpr02-api
```

By default it listens only on `127.0.0.1:8000`. Available routes are `/health`, `/v1/candles`, `/v1/quality` and `/v1/ticks/latest`. When no dataset exists, the API reports missing data instead of generating synthetic prices. Full setup, configuration and security notes are in [docs/OWN_API.md](docs/OWN_API.md). The API has no trading/order routes.

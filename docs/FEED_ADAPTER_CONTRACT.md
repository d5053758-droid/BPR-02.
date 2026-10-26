# BPR-02 feed adapter contract

## Purpose and status

This document defines the interface a future data-source adapter must use to feed BPR-02's signal bridge. The repository currently includes a historical CSV replay adapter and a JSON Lines signal bridge. **It does not include a Pocket Option connection or live order execution.**

Before implementing a platform-specific adapter, obtain current written confirmation from the provider that the proposed programmatic data access is authorized, plus the official interface/authentication specification. Pocket Option's public offer describes quotes calculated by its own quotation process and reserves the right to cancel or revise operations performed using unauthorized bot software. See the official public offer: https://pocketoption.com/es/public-offer. Do not use undocumented endpoints or send account credentials, cookies or session tokens to third-party libraries.

## Adapter boundary

A provider adapter should only do these things:

1. Obtain market data using a provider-authorized method.
2. Normalize each fully closed candle into the schema below.
3. Emit one JSON object per line to the standard input of bpr02-stream.
4. Preserve the provider's symbol/instrument identifier and actual candle-close timestamp.
5. Report disconnects, stale data, malformed candles and duplicate/out-of-order timestamps without silently inventing prices.

An adapter must not call trading/order endpoints in this project. The bridge outputs research alerts only.

## JSON Lines candle schema

| Field | Type | Rules |
|---|---|---|
| timestamp | string | ISO-8601 timestamp; timezone should be explicit |
| open | number | finite OHLC price |
| high | number | at least open, close and low |
| low | number | at most open, close and high |
| close | number | finite OHLC price |
| closed | boolean | must be true; in-progress candles are rejected |

Optional descriptive fields: symbol, timeframe, source. Use actual provider names, not guessed labels.

Example (illustrative values only):

    {"timestamp":"2025-01-01T00:00:00Z","open":1.04,"high":1.041,"low":1.039,"close":1.0405,"closed":true,"symbol":"EURUSD_OTC","timeframe":"M1","source":"provider_name"}

EURUSD_OTC is only an example identifier, not evidence that a feed offers that instrument or matches Pocket Option quotes.

## Pipe a compatible source into the bridge

    your-authorized-adapter | bpr02-stream --lookback 12 --symbol EURUSD --timeframe M1 --events-out events.jsonl

The adapter's stdout must contain only JSON Lines candle records. Diagnostics belong on stderr. The bridge prints alert events to stdout and appends them to the event log. It rejects incomplete, duplicate, invalid and out-of-order candles.

To test without a live feed:

    bpr02-replay data.csv --symbol EURUSD --timeframe M1 | bpr02-stream --lookback 12 --events-out events.jsonl

The replay command is historical playback, not real time. It labels its input as historical CSV replay. When that source field is passed through, the bridge event inherits it.

## Requirements before a platform connector is accepted

- Written authorization and current official API/SDK documentation.
- Documented authentication with scoped permissions; no browser session impersonation.
- Explicit mapping for instrument, OTC status, timezone, candle-close semantics and price precision.
- A method to detect stale/missing data and reconnect safely without duplicating candle events.
- Historical parity tests against candles displayed by the intended platform/instrument.
- Secrets held in local environment variables or a secrets manager; never commit tokens to the public repository or print them in logs.
- Read-only data and alerts stay separated from all execution concerns.

## Price-feed caution

A normal EUR/USD feed from another broker must not be assumed to match EUR/USD OTC on Pocket Option. This difference can invalidate candle patterns and expiry results. If matching platform data cannot be obtained through a documented authorized interface, keep BPR-02 in historical/research mode and compare alerts manually.

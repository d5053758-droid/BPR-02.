# Free Forex feed (Twelve Data) — read-only

This is a no-subscription alternative for developing and testing the BPR-02 data and analysis pipeline. It reads the latest available EUR/USD 1-minute candle from Twelve Data's market-data API and writes one JSON line locally. It **does not connect to FxPro, does not read the FxPro MT5 account, and cannot place trades**.

## Free Basic plan

Twelve Data currently lists a Basic plan at USD 0/month, including real-time Forex market data, 8 API credits per minute and 800 API requests per day. Forex data is described as updating at least once per minute. Check the provider's current terms and quota before use:
- Pricing: https://twelvedata.com/pricing
- Forex API: https://twelvedata.com/forex
- API docs: https://twelvedata.com/docs

This is a market-data service, not a broker connection. Its quote and candles may differ from FxPro's EURUSD price, spread, trading session, or execution. Treat the data as external research input only. Do not use it to validate ultra-short expiry strategies or claim parity with FxPro/Pocket Option.

## Run from GitHub Codespaces

1. Create a free Basic account at Twelve Data and obtain an API key.
2. Add the API key as a private Codespaces environment secret named `TWELVEDATA_API_KEY`. Do not commit it, place it in the code, or paste it into chat.
3. From the repository root, run:

```bash
python integrations/free_forex_data/read_forex.py
```

The script uses only the Python standard library. It requests one EUR/USD candle at a time and appends the result to `integrations/free_forex_data/data/quotes.jsonl`, which is excluded from Git.

## Limits and safety

- One manual run makes one API request.
- This is a one-shot fetch, not a 24/7 service.
- It never imports trading libraries or submits orders.
- Do not place the API key into a public repository or a commit.
- Never mix these candles into a broker-specific backtest without labeling the source and validating time alignment and data differences.

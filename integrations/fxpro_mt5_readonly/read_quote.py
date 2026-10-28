"""Read one MT5 quote through MetaApi. This script never submits trades."""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from metaapi_cloud_sdk import MetaApi


BASE_DIR = Path(__file__).resolve().parent
OUTPUT_FILE = BASE_DIR / "data" / "quotes.jsonl"


def required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(
            f"Missing environment variable {name}. "
            "Set it in your private runtime secrets; do not paste it into source code."
        )
    return value


async def read_one_quote() -> dict[str, Any]:
    token = required_env("METAAPI_TOKEN")
    account_id = required_env("METAAPI_ACCOUNT_ID")
    symbol = os.environ.get("MT5_SYMBOL", "EURUSD").strip() or "EURUSD"

    api = MetaApi(token)
    account = await api.metatrader_account_api.get_account(account_id)

    # The account should already be deployed in MetaApi and use investor/read-only
    # credentials where supported. This script deliberately does not deploy it.
    await account.wait_connected()

    connection = account.get_streaming_connection()
    try:
        await connection.connect()
        await connection.wait_synchronized({"timeoutInSeconds": 180})
        await connection.subscribe_to_market_data(symbol)

        loop = asyncio.get_running_loop()
        deadline = loop.time() + 30
        price: Any = None
        while loop.time() < deadline:
            price = connection.terminal_state.price(symbol)
            if price:
                break
            await asyncio.sleep(1)

        if not price:
            raise RuntimeError(
                f"No quote received for symbol {symbol!r} within 30 seconds. "
                "Check the exact symbol name and whether the market is publishing prices."
            )

        if isinstance(price, dict):
            price_data = price
        else:
            price_data = {"raw": str(price)}

        return {
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "symbol": symbol,
            "quote": price_data,
            "connected": bool(connection.terminal_state.connected),
            "connected_to_broker": bool(connection.terminal_state.connected_to_broker),
            "mode": "read_only_quote_check",
            "orders_submitted": 0,
        }
    finally:
        await connection.close()


async def main() -> None:
    result = await read_one_quote()
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(result, ensure_ascii=False, default=str)
    with OUTPUT_FILE.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    print(line)
    print(f"Quote saved locally to: {OUTPUT_FILE}")
    print("Safety check: orders_submitted=0")


if __name__ == "__main__":
    asyncio.run(main())

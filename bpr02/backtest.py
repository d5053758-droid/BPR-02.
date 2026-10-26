"""Conservative next-candle-open fixed-expiry backtester."""
from __future__ import annotations

from typing import Literal

import pandas as pd

from .signals import CALL, NO_TRADE, PUT

TiePolicy = Literal["push", "loss"]


def backtest_fixed_expiry(
    candles: pd.DataFrame,
    signals: pd.Series | pd.DataFrame,
    *,
    expiry_bars: int = 1,
    payout: float = 0.80,
    stake: float = 1.0,
    tie_policy: TiePolicy = "push",
) -> pd.DataFrame:
    """Backtest directional candidates against OHLC candles.

    A signal on candle i is entered at candle i+1 open. For expiry_bars=1,
    settlement is the close of that entry candle. Higher expiry values settle
    at the close of a later candle. Payout is net profit per unit stake on a
    win; loss is -stake. A tie is zero by default or -stake if tie_policy=loss.

    This assumes next-open fills and does not model OTC quote differences,
    latency, changing payouts or platform settlement. Research estimates only.
    """
    if expiry_bars < 1:
        raise ValueError("expiry_bars must be >= 1")
    if not 0 <= payout <= 1:
        raise ValueError("payout must be between 0 and 1")
    if stake <= 0:
        raise ValueError("stake must be > 0")
    if tie_policy not in ("push", "loss"):
        raise ValueError("tie_policy must be 'push' or 'loss'")
    required = {"timestamp", "open", "close"}
    if not required.issubset(candles.columns):
        raise ValueError(f"candles must contain {sorted(required)}")

    if isinstance(signals, pd.DataFrame):
        if "signal" not in signals.columns:
            raise ValueError("signals DataFrame must contain a 'signal' column")
        signal_series = signals["signal"]
    else:
        signal_series = signals
    if len(signal_series) != len(candles):
        raise ValueError("signals and candles must have the same number of rows")

    frame = candles.reset_index(drop=True)
    signal_series = signal_series.reset_index(drop=True)
    trades: list[dict[str, object]] = []
    for signal_idx, raw_signal in enumerate(signal_series):
        direction = str(raw_signal).strip().upper()
        if direction not in (CALL, PUT):
            continue
        entry_idx = signal_idx + 1
        expiry_idx = entry_idx + expiry_bars - 1
        if entry_idx >= len(frame) or expiry_idx >= len(frame):
            continue

        entry_price = float(frame.at[entry_idx, "open"])
        expiry_price = float(frame.at[expiry_idx, "close"])
        if direction == CALL:
            outcome = "WIN" if expiry_price > entry_price else "LOSS" if expiry_price < entry_price else "TIE"
        else:
            outcome = "WIN" if expiry_price < entry_price else "LOSS" if expiry_price > entry_price else "TIE"

        if outcome == "WIN":
            pnl = stake * payout
        elif outcome == "TIE" and tie_policy == "push":
            pnl = 0.0
        else:
            if outcome == "TIE" and tie_policy == "loss":
                outcome = "LOSS"
            pnl = -stake

        trades.append({
            "signal_timestamp": frame.at[signal_idx, "timestamp"],
            "entry_timestamp": frame.at[entry_idx, "timestamp"],
            "expiry_timestamp": frame.at[expiry_idx, "timestamp"],
            "direction": direction,
            "entry_price": entry_price,
            "expiry_price": expiry_price,
            "outcome": outcome,
            "stake": float(stake),
            "payout": float(payout),
            "pnl": float(pnl),
        })

    columns = [
        "signal_timestamp", "entry_timestamp", "expiry_timestamp", "direction",
        "entry_price", "expiry_price", "outcome", "stake", "payout", "pnl",
    ]
    results = pd.DataFrame(trades, columns=columns)
    results["cumulative_pnl"] = results["pnl"].cumsum()
    return results


def summarize_results(results: pd.DataFrame) -> dict[str, float | int | None]:
    """Calculate descriptive metrics without implying future profitability."""
    if not {"outcome", "pnl"}.issubset(results.columns):
        raise ValueError("results must contain 'outcome' and 'pnl' columns")

    total = int(len(results))
    wins = int((results["outcome"] == "WIN").sum())
    losses = int((results["outcome"] == "LOSS").sum())
    ties = int((results["outcome"] == "TIE").sum())
    settled = wins + losses
    pnl = pd.to_numeric(results["pnl"], errors="coerce").fillna(0.0)
    total_pnl = float(pnl.sum()) if total else 0.0
    gross_profit = float(pnl[pnl > 0].sum())
    gross_loss = float(-pnl[pnl < 0].sum())
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else None

    # Include the initial zero-equity point so first-trade losses count as drawdown.
    equity = pd.concat(
        [pd.Series([0.0]), pnl.reset_index(drop=True).cumsum()],
        ignore_index=True,
    )
    max_drawdown = float((equity - equity.cummax()).min()) if total else 0.0

    break_even_rate: float | None = None
    if "payout" in results.columns and total:
        payout_values = pd.to_numeric(results["payout"], errors="coerce").dropna()
        if not payout_values.empty and payout_values.mean() >= 0:
            # Theoretical non-tie break-even rate for the average modeled payout.
            break_even_rate = float(1.0 / (1.0 + payout_values.mean()))

    return {
        "trades": total,
        "wins": wins,
        "losses": losses,
        "ties": ties,
        "win_rate_excluding_ties": (wins / settled) if settled else None,
        "break_even_win_rate_excluding_ties": break_even_rate,
        "profit_factor": profit_factor,
        "net_pnl_in_stake_units": total_pnl,
        "expectancy_per_trade": (total_pnl / total) if total else None,
        "max_drawdown_in_stake_units": max_drawdown,
    }

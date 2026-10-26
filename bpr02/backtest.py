"""Conservative next-candle-open fixed-expiry backtester."""
from __future__ import annotations

from math import isfinite
from typing import Literal

import pandas as pd

from .signals import CALL, NO_TRADE, PUT
from .statistics import wilson_interval
from .time_quality import continuity_segments

TiePolicy = Literal["push", "loss"]


def _validate_backtest_inputs(
    candles: pd.DataFrame,
    signals: pd.Series | pd.DataFrame,
    *,
    expiry_bars: int,
    payout: float,
    stake: float,
    tie_policy: TiePolicy,
) -> pd.Series:
    """Fail closed on malformed candles, timestamps, parameters, or labels."""
    if isinstance(expiry_bars, bool) or not isinstance(expiry_bars, int) or expiry_bars < 1:
        raise ValueError("expiry_bars must be an integer >= 1")
    if isinstance(payout, bool) or not isinstance(payout, (int, float)):
        raise ValueError("payout must be a finite number between 0 and 1")
    if not isfinite(float(payout)) or not 0 <= float(payout) <= 1:
        raise ValueError("payout must be a finite number between 0 and 1")
    if isinstance(stake, bool) or not isinstance(stake, (int, float)):
        raise ValueError("stake must be a finite positive number")
    if not isfinite(float(stake)) or float(stake) <= 0:
        raise ValueError("stake must be a finite positive number")
    if tie_policy not in ("push", "loss"):
        raise ValueError("tie_policy must be 'push' or 'loss'")

    required = {"timestamp", "open", "high", "low", "close"}
    missing = sorted(required.difference(candles.columns))
    if missing:
        raise ValueError(f"candles must contain {sorted(required)}; missing {missing}")
    if candles.empty:
        raise ValueError("candles must contain at least one row")

    try:
        timestamps = pd.to_datetime(candles["timestamp"], errors="raise", utc=True)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"timestamps must be valid datetime values: {exc}") from exc
    if timestamps.isna().any():
        raise ValueError("timestamps must not be missing")
    if timestamps.duplicated().any():
        raise ValueError("timestamps must be unique; duplicate candle timestamp found")
    if not timestamps.is_monotonic_increasing:
        raise ValueError("candles must be chronologically sorted")

    numeric: dict[str, pd.Series] = {}
    for column in ("open", "high", "low", "close"):
        try:
            values = pd.to_numeric(candles[column], errors="raise")
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{column} prices must be numeric and finite") from exc
        if values.isna().any():
            raise ValueError(f"{column} prices must be numeric and finite")
        if not values.map(lambda value: isfinite(float(value))).all():
            raise ValueError(f"{column} prices must be numeric and finite")
        numeric[column] = values.astype(float)

    bad_high = numeric["high"] < pd.concat(
        [numeric["open"], numeric["close"], numeric["low"]], axis=1
    ).max(axis=1)
    bad_low = numeric["low"] > pd.concat(
        [numeric["open"], numeric["close"], numeric["high"]], axis=1
    ).min(axis=1)
    if (bad_high | bad_low).any():
        rows = [position + 1 for position, is_invalid in enumerate((bad_high | bad_low).to_numpy()) if is_invalid][:5]
        raise ValueError(f"invalid OHLC candle range at row(s): {rows}")

    if isinstance(signals, pd.DataFrame):
        if "signal" not in signals.columns:
            raise ValueError("signals DataFrame must contain a 'signal' column")
        signal_series = signals["signal"]
    else:
        signal_series = signals
    if len(signal_series) != len(candles):
        raise ValueError("signals and candles must have the same number of rows")

    normalized = signal_series.astype(str).str.strip().str.upper()
    permitted = {CALL, PUT, NO_TRADE}
    invalid_labels = sorted(set(normalized.unique()).difference(permitted))
    if invalid_labels:
        raise ValueError(f"signals contain unsupported label(s): {invalid_labels}")
    return normalized


def backtest_fixed_expiry(
    candles: pd.DataFrame,
    signals: pd.Series | pd.DataFrame,
    *,
    expiry_bars: int = 1,
    expected_interval_seconds: float | None = None,
    payout: float = 0.80,
    stake: float = 1.0,
    tie_policy: TiePolicy = "push",
) -> pd.DataFrame:
    """Backtest directional candidates against validated OHLC candles.

    A signal on candle i is entered at candle i+1 open. For expiry_bars=1,
    settlement is the close of that entry candle. Higher expiry values settle
    at the close of a later candle. Payout is net profit per unit stake on a
    win; loss is -stake. A tie is zero by default or -stake if tie_policy=loss.
    Any trade whose signal-to-settlement window crosses a timestamp gap is
    omitted. Expected candle spacing is inferred by default or can be supplied
    explicitly with expected_interval_seconds.

    Direct callers receive the same structural validation as CSV-loaded inputs:
    timestamps must be unique and chronological and every OHLC value finite.
    This assumes next-open fills and does not model OTC quote differences,
    latency, changing payouts or platform settlement. Research estimates only.
    """
    signal_series = _validate_backtest_inputs(
        candles,
        signals,
        expiry_bars=expiry_bars,
        payout=payout,
        stake=stake,
        tie_policy=tie_policy,
    )

    frame = candles.copy().reset_index(drop=True)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="raise", utc=True)
    continuity, _, _ = continuity_segments(
        frame, expected_interval_seconds=expected_interval_seconds
    )
    continuity = continuity.reset_index(drop=True)
    for column in ("open", "high", "low", "close"):
        frame[column] = pd.to_numeric(frame[column], errors="raise").astype(float)
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
        # Never model one trade across a timestamp gap. Otherwise a missing
        # minute could be mistaken for the next candle in a bar-based expiry.
        if continuity.iloc[signal_idx] != continuity.iloc[expiry_idx]:
            continue

        entry_price = float(frame.at[entry_idx, "open"])
        expiry_price = float(frame.at[expiry_idx, "close"])
        if direction == CALL:
            outcome = "WIN" if expiry_price > entry_price else "LOSS" if expiry_price < entry_price else "TIE"
        else:
            outcome = "WIN" if expiry_price < entry_price else "LOSS" if expiry_price > entry_price else "TIE"

        if outcome == "WIN":
            pnl = float(stake) * float(payout)
        elif outcome == "TIE" and tie_policy == "push":
            pnl = 0.0
        else:
            if outcome == "TIE" and tie_policy == "loss":
                outcome = "LOSS"
            pnl = -float(stake)

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
    win_rate_ci_low, win_rate_ci_high = wilson_interval(wins, settled)
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
        "win_rate_ci95_lower": win_rate_ci_low,
        "win_rate_ci95_upper": win_rate_ci_high,
        "break_even_win_rate_excluding_ties": break_even_rate,
        "profit_factor": profit_factor,
        "net_pnl_in_stake_units": total_pnl,
        "expectancy_per_trade": (total_pnl / total) if total else None,
        "max_drawdown_in_stake_units": max_drawdown,
    }


def backtest_coverage_diagnostics(
    candles: pd.DataFrame,
    signals: pd.Series | pd.DataFrame,
    *,
    expiry_bars: int = 1,
    expected_interval_seconds: float | None = None,
) -> dict[str, int | float | None]:
    """Count directional signals that can and cannot be modeled from this bar series.

    A candidate is eligible only if its entry and settlement bars exist and
    its signal-to-settlement window remains inside one continuous timestamp
    segment. This is a data-coverage diagnostic, not a strategy performance
    metric. It makes exclusions visible instead of silently shrinking the
    backtest sample.
    """
    normalized = _validate_backtest_inputs(
        candles,
        signals,
        expiry_bars=expiry_bars,
        payout=0.80,
        stake=1.0,
        tie_policy="push",
    )
    frame = candles.copy().reset_index(drop=True)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="raise", utc=True)
    segments, interval, break_count = continuity_segments(
        frame, expected_interval_seconds=expected_interval_seconds
    )
    segments = segments.reset_index(drop=True)

    candidate_signals = 0
    eligible_signals = 0
    missing_future_bars = 0
    timestamp_gap_exclusions = 0
    for signal_idx, label in enumerate(normalized):
        if label not in (CALL, PUT):
            continue
        candidate_signals += 1
        entry_idx = signal_idx + 1
        expiry_idx = entry_idx + expiry_bars - 1
        if entry_idx >= len(frame) or expiry_idx >= len(frame):
            missing_future_bars += 1
            continue
        if segments.iloc[signal_idx] != segments.iloc[expiry_idx]:
            timestamp_gap_exclusions += 1
            continue
        eligible_signals += 1

    return {
        "candidate_signals": candidate_signals,
        "eligible_candidate_signals": eligible_signals,
        "excluded_due_to_timestamp_gaps": timestamp_gap_exclusions,
        "excluded_due_to_missing_future_bars": missing_future_bars,
        "timestamp_discontinuities": int(break_count),
        "expected_interval_seconds_used": interval,
        "candidate_coverage_ratio": (
            eligible_signals / candidate_signals if candidate_signals else None
        ),
        "coverage_ratio_excludes_no_signal_rows": True,
    }

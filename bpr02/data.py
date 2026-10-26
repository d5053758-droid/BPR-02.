"""Import and validate OHLC CSV data without modifying source files."""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

import pandas as pd

_TIMESTAMP_ALIASES = ("timestamp", "datetime", "date_time", "date", "time")


class DataValidationError(ValueError):
    """Raised when historical candles cannot be safely interpreted."""


def _find_timestamp_column(columns: Iterable[str]) -> str | None:
    by_lower = {column.lower().strip(): column for column in columns}
    for alias in _TIMESTAMP_ALIASES:
        if alias in by_lower:
            return by_lower[alias]
    return None


def load_ohlc_csv(
    path: str | Path,
    *,
    timestamp_column: str | None = None,
) -> pd.DataFrame:
    """Load candles and return normalized columns in chronological order.

    The result has a UTC-aware timestamp column plus numeric open, high, low
    and close columns. Optional volume is retained if present. Duplicate
    timestamps, non-finite prices and invalid ranges raise DataValidationError.
    """
    source = Path(path)
    if not source.is_file():
        raise FileNotFoundError(f"CSV file not found: {source}")
    try:
        frame = pd.read_csv(source)
    except Exception as exc:
        raise DataValidationError(f"Could not read CSV: {exc}") from exc
    if frame.empty:
        raise DataValidationError("CSV contains no candle rows.")

    frame = frame.rename(columns={c: str(c).strip().lower() for c in frame.columns})
    requested = timestamp_column.strip().lower() if timestamp_column else None
    if requested:
        if requested not in frame.columns:
            raise DataValidationError(
                f"Timestamp column {timestamp_column!r} not found. Available: {list(frame.columns)}"
            )
        ts_col = requested
    else:
        ts_col = _find_timestamp_column(frame.columns)
    if ts_col is None:
        raise DataValidationError(
            "No timestamp column found. Expected timestamp, datetime, date_time, date or time."
        )

    missing = [c for c in ("open", "high", "low", "close") if c not in frame.columns]
    if missing:
        raise DataValidationError(f"Missing required OHLC columns: {missing}")
    frame = frame.rename(columns={ts_col: "timestamp"})
    try:
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="raise", utc=True)
    except Exception as exc:
        raise DataValidationError(f"Invalid timestamp value: {exc}") from exc
    if frame["timestamp"].isna().any():
        raise DataValidationError("Timestamp column contains missing values.")
    if frame["timestamp"].duplicated().any():
        duplicates = frame.loc[frame["timestamp"].duplicated(), "timestamp"].astype(str).tolist()
        raise DataValidationError(f"Duplicate candle timestamps found, examples: {duplicates[:3]}")

    for column in ("open", "high", "low", "close"):
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
        values = frame[column]
        if values.isna().any() or not values.map(lambda x: abs(float(x)) != float("inf")).all():
            raise DataValidationError(f"Column {column!r} has missing or non-finite prices.")

    bad_high = frame["high"] < frame[["open", "close", "low"]].max(axis=1)
    bad_low = frame["low"] > frame[["open", "close", "high"]].min(axis=1)
    if (bad_high | bad_low).any():
        rows = (frame.index[bad_high | bad_low] + 2).tolist()
        raise DataValidationError(f"Invalid OHLC candle range at CSV row(s): {rows[:5]}")

    if "volume" in frame.columns:
        frame["volume"] = pd.to_numeric(frame["volume"], errors="coerce")
    frame = frame.sort_values("timestamp", kind="stable").reset_index(drop=True)
    preferred = ["timestamp", "open", "high", "low", "close"]
    return frame[preferred + [c for c in frame.columns if c not in preferred]]

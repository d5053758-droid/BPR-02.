"""Fail-closed interface boundary for a possible future execution adapter.

This module defines types only. It does not connect to a broker, read credentials,
send network requests, or place orders. The default adapter always rejects requests.
Nothing in the signal bridge or CLI calls this module.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from typing import Literal, Protocol, runtime_checkable

OrderSide = Literal["CALL", "PUT"]
ExecutionStatus = Literal["accepted", "filled", "rejected", "unknown"]


class ExecutionError(RuntimeError):
    """Base class for execution-boundary errors."""


class ExecutionNotEnabledError(ExecutionError):
    """Raised because no authorized execution adapter is enabled in BPR-02."""


@dataclass(frozen=True, slots=True)
class OrderIntent:
    """A proposed order after a separate, explicit human approval.

    Creating this value does not submit anything. `approval_reference` must point
    to an external, auditable human-approval record; a model-generated signal alone
    must never be treated as approval.
    """

    event_id: str
    approval_reference: str
    symbol: str
    timeframe: str
    source: str
    signal_timestamp: str
    side: OrderSide
    stake: float
    expiry_seconds: int

    def __post_init__(self) -> None:
        required_text = {
            "event_id": self.event_id,
            "approval_reference": self.approval_reference,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "source": self.source,
            "signal_timestamp": self.signal_timestamp,
        }
        for field, value in required_text.items():
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field} must be a non-empty string")
        for field in ("symbol", "timeframe", "source"):
            if required_text[field].strip().upper() in {"UNKNOWN", "UNSET", "N/A"}:
                raise ValueError(f"{field} must identify the actual instrument/feed")

        if self.side not in ("CALL", "PUT"):
            raise ValueError("side must be CALL or PUT; NO TRADE is not executable")
        if isinstance(self.stake, bool) or not isinstance(self.stake, (int, float)):
            raise ValueError("stake must be a finite positive number")
        if not isfinite(float(self.stake)) or float(self.stake) <= 0:
            raise ValueError("stake must be a finite positive number")
        if isinstance(self.expiry_seconds, bool) or not isinstance(self.expiry_seconds, int):
            raise ValueError("expiry_seconds must be a positive integer")
        if self.expiry_seconds <= 0:
            raise ValueError("expiry_seconds must be a positive integer")

        try:
            timestamp = datetime.fromisoformat(self.signal_timestamp.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("signal_timestamp must be a valid ISO-8601 timestamp") from exc
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("signal_timestamp must include an explicit timezone")


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """Normalized outcome returned by a future authorized adapter."""

    status: ExecutionStatus
    adapter: str
    detail: str
    external_order_id: str | None = None


@runtime_checkable
class ExecutionAdapter(Protocol):
    """Contract for a future adapter authorized by its provider.

    Implementations must document their permissions, enforce risk limits, and
    report ambiguous timeouts as 'unknown' rather than blindly retrying orders.
    """

    def submit(self, intent: OrderIntent) -> ExecutionResult:
        """Submit one explicitly approved intent through the configured provider."""
        ...


class DisabledExecutionAdapter:
    """Default fail-closed adapter; never sends orders or performs network I/O."""

    name = "disabled"

    def submit(self, intent: OrderIntent) -> ExecutionResult:
        del intent  # Deliberately unused; this implementation has no side effects.
        raise ExecutionNotEnabledError(
            "Execution is disabled: BPR-02 has no configured, authorized broker adapter. "
            "This research-only project does not submit orders."
        )

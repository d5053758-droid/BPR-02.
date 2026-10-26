import pytest

from bpr02.execution import (
    DisabledExecutionAdapter,
    ExecutionAdapter,
    ExecutionNotEnabledError,
    OrderIntent,
)


def valid_intent(**overrides):
    values = {
        "event_id": "evt-001",
        "approval_reference": "operator-review-2026-001",
        "symbol": "EURUSD_OTC",
        "timeframe": "M1",
        "source": "verified_demo_feed",
        "signal_timestamp": "2026-10-26T18:00:00-03:00",
        "side": "CALL",
        "stake": 1.0,
        "expiry_seconds": 60,
    }
    values.update(overrides)
    return OrderIntent(**values)


def test_disabled_adapter_implements_contract_and_always_fails_closed():
    adapter = DisabledExecutionAdapter()
    intent = valid_intent()

    assert isinstance(adapter, ExecutionAdapter)
    with pytest.raises(ExecutionNotEnabledError, match="Execution is disabled"):
        adapter.submit(intent)


@pytest.mark.parametrize("side", ["NO TRADE", "BUY", "SELL", "", None])
def test_order_intent_rejects_non_directional_or_unknown_side(side):
    with pytest.raises(ValueError, match="side must be CALL or PUT"):
        valid_intent(side=side)


@pytest.mark.parametrize("stake", [0, -1, float("inf"), float("nan"), True])
def test_order_intent_rejects_invalid_stake(stake):
    with pytest.raises(ValueError, match="stake must be a finite positive number"):
        valid_intent(stake=stake)


@pytest.mark.parametrize("expiry_seconds", [0, -1, 1.5, True])
def test_order_intent_rejects_invalid_expiry(expiry_seconds):
    with pytest.raises(ValueError, match="expiry_seconds must be a positive integer"):
        valid_intent(expiry_seconds=expiry_seconds)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("event_id", ""),
        ("approval_reference", ""),
        ("symbol", "UNKNOWN"),
        ("timeframe", "UNSET"),
        ("source", "N/A"),
    ],
)
def test_order_intent_requires_auditable_identity(field, value):
    with pytest.raises(ValueError):
        valid_intent(**{field: value})


@pytest.mark.parametrize(
    "signal_timestamp",
    [
        "2026-10-26T18:00:00",
        "not-a-timestamp",
    ],
)
def test_order_intent_requires_timestamp_with_timezone(signal_timestamp):
    with pytest.raises(ValueError):
        valid_intent(signal_timestamp=signal_timestamp)

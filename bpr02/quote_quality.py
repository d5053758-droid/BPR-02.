"""Shared heuristic assessment for timestamped quote sampling resolution."""
from __future__ import annotations

import math
from typing import Any


def assess_quote_temporal_resolution(
    *,
    observations: int,
    span_seconds: float,
    p95_gap_seconds: float | None,
    expiry_seconds: float,
    target_resolution_fraction: float = 0.25,
) -> dict[str, Any]:
    """Label quote sampling quality relative to an expiry without claiming validity.

    The p95-gap target is a configurable screening heuristic, not a universal
    threshold and not evidence of exact execution/settlement or market parity.
    """
    if isinstance(observations, bool) or not isinstance(observations, int) or observations < 0:
        raise ValueError("observations must be an integer >= 0")
    if (
        isinstance(span_seconds, bool)
        or not isinstance(span_seconds, (int, float))
        or not math.isfinite(float(span_seconds))
        or span_seconds < 0
    ):
        raise ValueError("span_seconds must be finite and >= 0")
    if (
        isinstance(expiry_seconds, bool)
        or not isinstance(expiry_seconds, (int, float))
        or not math.isfinite(float(expiry_seconds))
        or expiry_seconds <= 0
    ):
        raise ValueError("expiry_seconds must be finite and > 0")
    if (
        isinstance(target_resolution_fraction, bool)
        or not isinstance(target_resolution_fraction, (int, float))
        or not math.isfinite(float(target_resolution_fraction))
        or not 0 < target_resolution_fraction <= 1
    ):
        raise ValueError("target_resolution_fraction must be finite and in (0, 1]")
    if p95_gap_seconds is not None and (
        isinstance(p95_gap_seconds, bool)
        or not isinstance(p95_gap_seconds, (int, float))
        or not math.isfinite(float(p95_gap_seconds))
        or p95_gap_seconds < 0
    ):
        raise ValueError("p95_gap_seconds must be finite and >= 0 when provided")

    target_gap = float(expiry_seconds) * float(target_resolution_fraction)
    if observations < 2:
        label = "insufficient_observations"
    elif span_seconds < expiry_seconds:
        label = "insufficient_time_coverage"
    elif p95_gap_seconds is not None and p95_gap_seconds <= target_gap:
        label = "meets_configured_resolution_screen"
    elif p95_gap_seconds is not None and p95_gap_seconds <= expiry_seconds / 2:
        label = "limited_resolution_review_quote_delays"
    else:
        label = "low_temporal_resolution_for_requested_expiry"

    return {
        "target_resolution_fraction_of_expiry": float(target_resolution_fraction),
        "target_p95_gap_seconds": target_gap,
        "temporal_resolution_assessment": label,
        "assessment_is_heuristic": True,
    }

import pytest

from bpr02.quote_quality import assess_quote_temporal_resolution


def test_quote_resolution_screen_is_heuristic_and_expiry_relative():
    fine = assess_quote_temporal_resolution(
        observations=100,
        span_seconds=10,
        p95_gap_seconds=0.1,
        expiry_seconds=3,
        target_resolution_fraction=0.25,
    )
    assert fine["target_p95_gap_seconds"] == pytest.approx(0.75)
    assert fine["temporal_resolution_assessment"] == "meets_configured_resolution_screen"
    assert fine["assessment_is_heuristic"] is True

    whole_second = assess_quote_temporal_resolution(
        observations=20,
        span_seconds=19,
        p95_gap_seconds=1.0,
        expiry_seconds=3,
        target_resolution_fraction=0.25,
    )
    assert whole_second["temporal_resolution_assessment"] == "limited_resolution_review_quote_delays"


def test_quote_resolution_screen_distinguishes_insufficient_duration():
    result = assess_quote_temporal_resolution(
        observations=10,
        span_seconds=1,
        p95_gap_seconds=0.1,
        expiry_seconds=3,
    )
    assert result["temporal_resolution_assessment"] == "insufficient_time_coverage"


@pytest.mark.parametrize(
    ("observations", "span", "p95", "expiry", "fraction"),
    [
        (-1, 1, 0.1, 3, 0.25),
        (2, -1, 0.1, 3, 0.25),
        (2, 1, -0.1, 3, 0.25),
        (2, 1, 0.1, 0, 0.25),
        (2, 1, 0.1, 3, 0),
        (2, 1, 0.1, 3, 1.1),
    ],
)
def test_quote_resolution_screen_rejects_invalid_inputs(observations, span, p95, expiry, fraction):
    with pytest.raises(ValueError):
        assess_quote_temporal_resolution(
            observations=observations,
            span_seconds=span,
            p95_gap_seconds=p95,
            expiry_seconds=expiry,
            target_resolution_fraction=fraction,
        )

import pytest

from bpr02.statistics import wilson_interval


def test_wilson_interval_is_centered_and_inside_probability_bounds():
    low, high = wilson_interval(5, 10)
    assert low is not None and high is not None
    assert 0 < low < 0.5 < high < 1
    assert low + high == pytest.approx(1.0)


def test_wilson_interval_handles_zero_and_all_successes():
    zero_low, zero_high = wilson_interval(0, 10)
    all_low, all_high = wilson_interval(10, 10)
    assert zero_low == 0
    assert zero_high is not None and 0 < zero_high < 1
    assert all_low is not None and 0 < all_low < 1
    assert all_high == 1
    assert wilson_interval(0, 0) == (None, None)


def test_wilson_interval_rejects_invalid_counts_and_z_score():
    with pytest.raises(ValueError):
        wilson_interval(11, 10)
    with pytest.raises(ValueError):
        wilson_interval(0, -1)
    with pytest.raises(ValueError):
        wilson_interval(1, 5, z_score=0)

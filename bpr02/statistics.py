"""Statistical summaries for small-sample backtest proportions."""
from __future__ import annotations

from math import isfinite, sqrt


def wilson_interval(
    successes: int,
    trials: int,
    *,
    z_score: float = 1.96,
) -> tuple[float | None, float | None]:
    """Return a Wilson score interval for a binomial proportion.

    The default z-score (1.96) gives the usual approximate 95% interval.
    With zero trials, returns (None, None). This interval describes uncertainty
    in the observed win proportion only; it is not a confidence interval for
    trading profit, payout parity or future performance.
    """
    if trials < 0:
        raise ValueError("trials must be >= 0")
    if successes < 0 or successes > trials:
        raise ValueError("successes must be between 0 and trials")
    if not isfinite(z_score) or z_score <= 0:
        raise ValueError("z_score must be a finite number greater than zero")
    if trials == 0:
        return None, None

    n = float(trials)
    observed = successes / n
    z2 = z_score * z_score
    denominator = 1.0 + z2 / n
    center = (observed + z2 / (2.0 * n)) / denominator
    margin = (
        z_score
        * sqrt(observed * (1.0 - observed) / n + z2 / (4.0 * n * n))
        / denominator
    )
    return max(0.0, center - margin), min(1.0, center + margin)

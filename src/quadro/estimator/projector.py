"""Statistical projection from calibration data.

Experimental internal API. The default projector uses sample mean and standard
deviation with t-distribution critical values from small lookup tables. It uses
only the stdlib ``statistics`` module; no SciPy dependency is required.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass

from .calibration import Calibration
from .pricing import Pricing

_Z_VALUES = {0.90: 1.645, 0.95: 1.960, 0.99: 2.576}
_T_VALUES = {
    0.90: {
        1: 6.314,
        2: 2.920,
        3: 2.353,
        4: 2.132,
        5: 2.015,
        6: 1.943,
        7: 1.895,
        8: 1.860,
        9: 1.833,
        10: 1.812,
        15: 1.753,
        20: 1.725,
        25: 1.708,
        29: 1.699,
    },
    0.95: {
        1: 12.706,
        2: 4.303,
        3: 3.182,
        4: 2.776,
        5: 2.571,
        6: 2.447,
        7: 2.365,
        8: 2.306,
        9: 2.262,
        10: 2.228,
        15: 2.131,
        20: 2.086,
        25: 2.060,
        29: 2.045,
    },
    0.99: {
        1: 63.657,
        2: 9.925,
        3: 5.841,
        4: 4.604,
        5: 4.032,
        6: 3.707,
        7: 3.499,
        8: 3.355,
        9: 3.250,
        10: 3.169,
        15: 2.947,
        20: 2.845,
        25: 2.787,
        29: 2.756,
    },
}


@dataclass(frozen=True)
class Projection:
    """The result of projecting calibration data to a queue size."""

    n_tasks: int
    confidence: float
    total_tokens: int
    total_tokens_low: int
    total_tokens_high: int
    by_stage: dict[str, int]
    mean_tokens_per_task: float
    stdev_tokens_per_task: float
    coefficient_of_variation: float
    total_dollars: float | None
    total_dollars_low: float | None
    total_dollars_high: float | None
    samples_used: int
    sample_cost_dollars: float | None
    pricing_source: str | None
    pricing_verify_url: str | None

    @property
    def variance_warning(self) -> bool:
        return self.coefficient_of_variation > 0.30


@dataclass
class Projector:
    """Mean/stdev projector with prediction intervals.

    The interval is computed as a *prediction interval for the sum* of N
    future i.i.d. tasks, given sample mean and sample stdev estimated from
    n calibration samples. The margin is::

        margin = t × stdev × sqrt(N + N**2 / n)

    where t is the critical value for the chosen confidence level at n-1
    degrees of freedom. The two terms inside the sqrt have distinct
    interpretations:

    * The ``N`` term is the per-task variability propagated to the sum
      (standard error of the sum, given known population parameters).
    * The ``N**2 / n`` term captures parameter uncertainty: we estimated
      the mean from only n samples, so the same mean estimate is being
      multiplied across N future tasks. This term dominates when N >> n,
      which is exactly the situation where users would otherwise be
      misled by an artificially tight CI on a small-sample extrapolation.

    The earlier shipped Estimator used ``margin = t * stdev * sqrt(N)``,
    which is correct for known parameters but ignores estimation
    uncertainty. The new formula compounds both sources, producing
    intervals that widen visibly when extrapolating from a small sample
    to a large queue. This is intended; honest projections beat tight
    projections every time.
    """

    confidence: float = 0.95

    def __post_init__(self) -> None:
        if not 0.5 <= self.confidence <= 0.99:
            raise ValueError("confidence must be between 0.5 and 0.99")

    def project(
        self,
        calibration: Calibration,
        n_tasks: int,
        pricing: Pricing | None,
        sample_cost_dollars: float | None,
    ) -> Projection:
        if calibration.n < 2:
            raise ValueError(
                "Cannot project with fewer than 2 calibration samples; "
                "variance is undefined."
            )
        if n_tasks < 0:
            raise ValueError("n_tasks must be >= 0")

        per_task = [task.total_tokens for task in calibration.tasks]
        mean = statistics.mean(per_task)
        stdev = statistics.stdev(per_task)
        cov = stdev / mean if mean > 0 else 0.0
        total_mean = mean * n_tasks
        # Prediction-interval-of-the-sum, accounting for both per-task
        # variability AND parameter uncertainty from finite sampling.
        # See class docstring for derivation.
        n_samples = calibration.n
        spread_factor = math.sqrt(n_tasks + (n_tasks * n_tasks) / n_samples)
        margin = self._critical_value(n_samples - 1) * stdev * spread_factor
        low = max(0, int(total_mean - margin))
        high = int(total_mean + margin)

        dollars: float | None = None
        dollars_low: float | None = None
        dollars_high: float | None = None
        pricing_source: str | None = None
        verify_url: str | None = None
        if pricing is not None:
            model = self._dominant_model(calibration)
            dollars = pricing.cost_for_tokens(model, int(total_mean))
            dollars_low = pricing.cost_for_tokens(model, low)
            dollars_high = pricing.cost_for_tokens(model, high)
            pricing_source = pricing.source_label
            verify_url = pricing.verify_url

        return Projection(
            n_tasks=n_tasks,
            confidence=self.confidence,
            total_tokens=int(total_mean),
            total_tokens_low=low,
            total_tokens_high=high,
            by_stage=self._project_by_stage(calibration, n_tasks),
            mean_tokens_per_task=mean,
            stdev_tokens_per_task=stdev,
            coefficient_of_variation=cov,
            total_dollars=dollars,
            total_dollars_low=dollars_low,
            total_dollars_high=dollars_high,
            samples_used=calibration.n,
            sample_cost_dollars=sample_cost_dollars,
            pricing_source=pricing_source,
            pricing_verify_url=verify_url,
        )

    def _critical_value(self, df: int) -> float:
        if df >= 30:
            return _Z_VALUES.get(round(self.confidence, 2), 1.96)
        table = _T_VALUES.get(round(self.confidence, 2))
        if table is None:
            return _Z_VALUES.get(round(self.confidence, 2), 1.96)
        if df in table:
            return table[df]
        nearest = max(k for k in table if k <= df)
        return table[nearest]

    @staticmethod
    def _project_by_stage(calibration: Calibration, n_tasks: int) -> dict[str, int]:
        result: dict[str, int] = {}
        for stage in calibration.all_stages:
            values = [task.by_stage.get(stage, 0) for task in calibration.tasks]
            result[stage] = int(statistics.mean(values) * n_tasks)
        return result

    @staticmethod
    def _dominant_model(calibration: Calibration) -> str:
        """Return the highest-token model, or ``default``.

        V1 uses a dominant-model heuristic because token records do not yet
        persist input/output splits. Multi-model dollar breakdown is a known
        limitation documented in ``RUN_NOTE.md``.
        """
        totals: dict[str, int] = {}
        for task in calibration.tasks:
            for model, tokens in task.by_model.items():
                totals[model] = totals.get(model, 0) + tokens
        if not totals:
            return "default"
        return max(totals.items(), key=lambda item: item[1])[0]

"""
Cost estimation for saga-backed pipelines.

The :class:`Estimator` class projects token and dollar costs for running a
pipeline against a queue of tasks. The recommended construction path is
:meth:`Estimator.from_dry_run`, which uses a two-pass approach: pass 1 walks
the queue without LLM calls to characterize input shapes, and pass 2 runs
samples spanning the distribution to measure actual token usage.

Variance is reported as a first-class output. Every projection includes a
confidence interval and a coefficient of variation; when CoV exceeds 0.30 the
formatter emits a variance warning recommending more samples.
"""

from .estimator import Estimator
from .pricing import ModelPricing, Pricing
from .projector import Projection

__all__ = ["Estimator", "ModelPricing", "Pricing", "Projection"]

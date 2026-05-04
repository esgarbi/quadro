"""Sample selection for pass 2 of the dry-run estimator.

Experimental internal API. The default strategy sorts observations by total
input characters and picks samples spanning the distribution: smallest,
largest, and evenly spaced middle samples.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class _SizedObservation(Protocol):
    @property
    def total_input_chars(self) -> int:
        ...


@dataclass
class SamplingStrategy:
    """Sort-and-bucket sampler for dry-run observations."""

    target_samples: int
    min_samples: int = 3

    def pick_indices(self, observations: list[_SizedObservation]) -> list[int]:
        """Return indices into the original observation list to sample."""
        if self.target_samples <= 0 or not observations:
            return []
        if len(observations) <= self.target_samples:
            return list(range(len(observations)))

        sorted_pairs = sorted(
            enumerate(observations),
            key=lambda pair: pair[1].total_input_chars,
        )
        picks_in_sorted = [0, len(sorted_pairs) - 1]
        remaining = max(0, self.target_samples - 2)
        if remaining > 0:
            stride = (len(sorted_pairs) - 2) / (remaining + 1)
            for i in range(1, remaining + 1):
                idx = int(round(i * stride)) + 1
                idx = min(max(1, idx), len(sorted_pairs) - 2)
                if idx not in picks_in_sorted:
                    picks_in_sorted.append(idx)

        return sorted(sorted_pairs[i][0] for i in picks_in_sorted)

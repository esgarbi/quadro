"""Calibration data structures for token measurements.

Experimental internal API. A :class:`Calibration` is the input to the
projector and is populated from either dry-run samples or historical Board
records.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class TaskCalibration:
    """Measured token usage for one sampled or historical task."""

    task_id: str
    total_tokens: int
    by_stage: dict[str, int] = field(default_factory=dict)
    by_step: dict[str, int] = field(default_factory=dict)
    by_model: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class Calibration:
    """Uniform calibration input consumed by the projector."""

    tasks: list[TaskCalibration]

    @property
    def n(self) -> int:
        return len(self.tasks)

    @property
    def all_stages(self) -> set[str]:
        result: set[str] = set()
        for task in self.tasks:
            result.update(task.by_stage.keys())
        return result

    @property
    def all_models(self) -> set[str]:
        result: set[str] = set()
        for task in self.tasks:
            result.update(task.by_model.keys())
        return result

"""
The built saga — an immutable plan produced by ``SagaBuilder.build()``.

A ``BuiltSaga`` is cheap to instantiate and trivially safe to share
across threads: every field is frozen and step order is fixed at build
time. The runner consumes it; it is never mutated.

The name ``BuiltSaga`` rather than ``Saga`` is deliberate. The short
name ``Saga`` is reserved at the package level (``quadro.saga.Saga``)
for the fluent builder, so user code reads
``Saga("ideation").step(...)`` rather than the longer
``SagaBuilder("ideation").step(...)``. Distinguishing the dataclass
from the builder at the class-definition level (instead of disambiguating
them with a re-export alias) keeps every name unambiguous everywhere it
appears in source — no IDE jump-to-definition surprises, no sphinx
autodoc collisions, no static-analysis confusion.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .steps import Step


@dataclass(frozen=True)
class BuiltSaga:
    """An immutable saga plan.

    Attributes
    ----------
    name:
        Human-readable identifier. Used in telemetry events and as a
        prefix on persistence keys for diagnostics.
    steps:
        The ordered tuple of steps. Order is the canonical "happy path"
        traversal order; gates and forks (added in later milestones)
        introduce branching by setting the runner's program counter
        explicitly rather than by reordering this tuple.
    saga_modifiers:
        Saga-wide modifiers attached at build time (``idempotent_by``,
        ``with_sla``, etc.). Empty in milestone A unless
        ``.idempotent(by=...)`` was called on the builder.
    compensations:
        ``step_name -> {"undo": callable, "on_failure": str}`` map.
        Registered by ``SagaBuilder.compensate(...)``. The ``undo``
        callable receives a ``SagaContext`` populated with the step's
        output under ``ctx.step[step_name]``. The ``on_failure`` entry
        is either ``"continue"`` (default — the rollback walker logs
        the failure and continues) or ``"halt"`` (the walker stops;
        remaining earlier compensations are NOT invoked). The type
        widened from the milestone-A ``dict[str, Callable]`` shape
        because milestone D needs to carry the per-compensation
        ``on_failure`` metadata alongside the callable; ``Option (a)``
        in the milestone-D brief's builder-section design choice.
    """

    name: str
    steps: tuple[Step, ...]
    saga_modifiers: dict[str, Any] = field(default_factory=dict)
    compensations: dict[str, dict[str, Any]] = field(default_factory=dict)

    def find(self, step_name: str) -> Step:
        """Locate a step by name. Raises ``KeyError`` if unknown."""
        for s in self.steps:
            if s.name == step_name:
                return s
        raise KeyError(f"saga {self.name!r} has no step named {step_name!r}")

    def next_after(self, step_name: str) -> str | None:
        """Return the name of the next step in declaration order, or
        ``None`` if ``step_name`` was the last step (saga complete).

        Raises ``KeyError`` if ``step_name`` is not in the saga.
        """
        for i, s in enumerate(self.steps):
            if s.name == step_name:
                return self.steps[i + 1].name if i + 1 < len(self.steps) else None
        raise KeyError(f"saga {self.name!r} has no step named {step_name!r}")

    def first_step(self) -> str | None:
        """Return the name of the first step, or ``None`` for an empty saga."""
        return self.steps[0].name if self.steps else None

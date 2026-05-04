"""
Step primitives for the saga DSL.

A ``Step`` is one unit of work in a saga — a frozen dataclass whose
``kind`` field identifies which dispatch path the saga runner takes.
``payload`` carries step-kind-specific configuration (the callable for
``deterministic``, the prompt and schema for ``reason``, the branches
for ``parallel``, etc.). ``modifiers`` carries cross-cutting decorators
(``deadline``, ``retry``, ``with_sla``) that the fluent builder attaches
via its "current step" pointer.

In milestone A only ``StepKind.DETERMINISTIC`` is dispatched by the
runner. The rest of the kinds are declared here so the builder API is
forward-compatible — adding ``reason`` in milestone B does not change
the type surface the runner consumes.

``SagaContext`` is the hydrated value passed to every step callable. It
exposes the current task, outputs of completed steps, and a ``now``
timestamp for evidence capture.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class StepKind(StrEnum):
    """Frozen taxonomy of step dispatch kinds.

    Milestone A dispatches only ``DETERMINISTIC``. Other values are
    declared here so the builder surface is stable across the rollout —
    later milestones add dispatch paths for them in the saga runner
    without changing the type.
    """

    DETERMINISTIC = "deterministic"
    REASON = "reason"               # milestone B
    GATE = "gate"                   # milestone C
    GUARD = "guard"                 # milestone C
    EVIDENCE = "evidence"           # milestone C
    STAMP = "stamp"                 # milestone C
    EXPECT = "expect"               # milestone C
    COMPENSATE = "compensate"       # milestone D
    PARALLEL = "parallel"           # milestone E
    FORK = "fork"                   # milestone F
    JOIN = "join"                   # milestone F


@dataclass(frozen=True)
class Step:
    """A single immutable step in a saga.

    Built by ``SagaBuilder`` and stored in ``Saga.steps``. The runner
    dispatches on ``kind`` and pulls step-kind-specific data from
    ``payload``. Modifiers attached via fluent methods (``.retry()``,
    ``.deadline()``, ``.with_sla()``) live in ``modifiers``.
    """

    name: str
    kind: StepKind
    payload: dict[str, Any] = field(default_factory=dict)
    modifiers: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BuiltBranch:
    """A single branch within a parallel step."""

    name: str
    steps: tuple[Step, ...]
    compensations: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass
class SagaContext:
    """Hydrated context passed to every step callable.

    Attributes
    ----------
    task:
        The current ``TaskRecord`` as a dict, exactly as the worker
        received it from the Chief's dispatch payload.
    step:
        Outputs of steps that have already completed in this saga run,
        keyed by step name. A step callable can read
        ``ctx.step["earlier_step"]`` to access an earlier result.
    evidence:
        Evidence captured by ``.evidence(...)`` steps earlier in the
        saga. Empty in milestone A; populated starting in milestone C.
    now:
        UTC timestamp captured at step dispatch. Stable for the
        duration of one step invocation; not refreshed mid-step.
    """

    task: dict[str, Any]
    step: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)
    now: datetime | None = None

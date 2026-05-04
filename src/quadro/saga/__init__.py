"""
Saga DSL — Quadro-native stage authoring.

A saga is an immutable, declarative plan for what happens inside one
pipeline stage. Where ``stage(workflow=...)`` and ``stage(graph=...)``
delegate stage execution to MAF or LangChain, ``stage(saga=...)`` keeps
stage execution inside Quadro's own runtime — with named steps, persisted
state, and a vocabulary of directives that compose with the existing
Board, lifecycle, and Sponsor contracts.

Vocabulary discipline: a Pipeline has stages, a Saga has steps, a
Lifecycle has phases. Each builder layer owns one primary noun, and
those nouns do not overlap. ``Saga.step(...)`` declares a unit of work
inside a saga; it is unrelated to ``LifecycleBuilder.phase(...)`` (which
declares a transition between two task statuses) and to
``Pipeline.stage(...)`` (which declares a slot that owns one lifecycle
transition).

Naming convention: ``Saga`` (the short, user-facing name at the
package level) is the fluent builder. ``BuiltSaga`` is the frozen
dataclass that ``Saga.build()`` returns. User code typically only
needs the builder; ``BuiltSaga`` shows up in type annotations on the
runtime side, in tests, and anywhere the structural shape of a
finished saga matters.

Milestone A exposes the minimal public surface needed for foundational
testing: ``Saga`` (the builder) plus the ``Step`` and ``SagaContext``
types that step callables receive. Subsequent milestones add
``reason``, ``gate``, ``parallel``, ``fork``, ``compensate``, and their
associated modifiers.
"""

from .builder import SagaBuilder
from .reasoner import Reasoner, ReasonResult
from .saga import BuiltSaga
from .state import SagaState
from .steps import BuiltBranch, SagaContext, Step, StepKind

# Public alias: user code reads ``Saga("ideation").step(...)`` rather
# than ``SagaBuilder("ideation").step(...)``. The two names point at
# the same class — ``Saga`` is the short ergonomic alias for the
# builder, while ``SagaBuilder`` remains available for code that
# prefers the explicit name.
Saga = SagaBuilder

__all__ = [
    "BuiltSaga",
    "BuiltBranch",
    "Reasoner",
    "ReasonResult",
    "Saga",
    "SagaBuilder",
    "SagaContext",
    "SagaState",
    "Step",
    "StepKind",
]

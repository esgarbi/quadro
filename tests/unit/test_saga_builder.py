from __future__ import annotations

from quadro.saga import BuiltSaga, Saga as SagaAlias  # top-level alias points to SagaBuilder
from quadro.saga.steps import StepKind

import pytest


def test_builder_creates_saga_with_correct_steps() -> None:
    """Two steps, declared in order, end up as a tuple in declaration order."""
    saga = (
        SagaAlias("test")
        .deterministic("first", lambda ctx: "a")
        .deterministic("second", lambda ctx: "b")
        .build()
    )
    assert isinstance(saga, BuiltSaga)
    assert saga.name == "test"
    assert [s.name for s in saga.steps] == ["first", "second"]
    assert all(s.kind == StepKind.DETERMINISTIC for s in saga.steps)


def test_builder_records_compensations() -> None:
    """Compensations are stored on the built Saga under a
    ``{undo, on_failure}`` dict. Before milestone D the record was a
    bare callable; milestone D widened the shape so the builder can
    carry per-compensation ``on_failure`` metadata ("continue" or
    "halt") alongside the callable."""
    # Defined as `def` rather than `lambda` to satisfy ruff's E731 lint;
    # functionally identical to `undo = lambda ctx: None`.
    def undo(ctx):
        return None

    saga = (
        SagaAlias("test")
        .deterministic("persist", lambda ctx: None)
        .compensate("persist", undo=undo)
        .build()
    )
    assert "persist" in saga.compensations
    record = saga.compensations["persist"]
    assert record["undo"] is undo
    # Default on_failure mode is "continue" per milestone-D Option 2.
    assert record["on_failure"] == "continue"


def test_builder_records_idempotency_key() -> None:
    """``.idempotent(by=...)`` populates ``saga_modifiers``."""
    saga = (
        SagaAlias("test")
        .idempotent(by="order_id")
        .deterministic("noop", lambda ctx: None)
        .build()
    )
    assert saga.saga_modifiers["idempotent_by"] == "order_id"


def test_builder_rejects_duplicate_step_names() -> None:
    """Step names are the primary key; duplicates raise at build time."""
    with pytest.raises(ValueError, match="duplicate step name"):
        (
            SagaAlias("test")
            .deterministic("same", lambda ctx: None)
            .deterministic("same", lambda ctx: None)
        )


def test_builder_rejects_compensation_for_unknown_step() -> None:
    """``.compensate("ghost", ...)`` for a step that was never declared
    raises at build time."""
    with pytest.raises(ValueError, match="references a step that was never declared"):
        (
            SagaAlias("test")
            .deterministic("real", lambda ctx: None)
            .compensate("ghost", undo=lambda ctx: None)
            .build()
        )


def test_builder_rejects_empty_saga() -> None:
    """A saga must have at least one step."""
    with pytest.raises(ValueError, match="has no steps"):
        SagaAlias("empty").build()


def test_built_saga_is_immutable() -> None:
    """``BuiltSaga`` is a frozen dataclass — direct mutation raises."""
    saga = SagaAlias("test").deterministic("x", lambda ctx: None).build()
    with pytest.raises(Exception):  # FrozenInstanceError or AttributeError
        saga.name = "renamed"


def test_saga_navigation_methods() -> None:
    """``find``, ``first_step``, and ``next_after`` walk the step tuple correctly."""
    saga = (
        SagaAlias("test")
        .deterministic("a", lambda ctx: 1)
        .deterministic("b", lambda ctx: 2)
        .deterministic("c", lambda ctx: 3)
        .build()
    )
    assert saga.first_step() == "a"
    assert saga.find("b").name == "b"
    assert saga.next_after("a") == "b"
    assert saga.next_after("b") == "c"
    assert saga.next_after("c") is None  # last step → saga complete

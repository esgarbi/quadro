from __future__ import annotations

import asyncio

from quadro.pipeline import StageSpec
from quadro.runtime_plugins.base import RuntimeContext
from quadro.runtime_plugins.saga import QuadroSagaRuntime
from quadro.saga import Saga as SagaAlias


def _fake_board_fn(store: dict) -> callable:
    """Tiny in-memory replacement for ``board_fn`` covering only the two
    intents the saga runtime uses in milestone A."""

    def _fn(intent: str, payload: dict) -> dict:
        if intent == "board.put_data":
            store[payload["key"]] = payload["value"]
            return {"ok": True}
        if intent == "board.get_data":
            return {"key": payload["key"], "value": store.get(payload["key"])}
        raise AssertionError(f"unexpected intent in milestone-A test: {intent}")

    return _fn


def _ctx(spec: StageSpec, task: dict, board_fn) -> RuntimeContext:
    return RuntimeContext(
        stage=spec,
        task=task,
        context={"payload": {"task": task}},
        board_fn=board_fn,
    )


def test_runtime_can_handle_only_saga_specs() -> None:
    """``can_handle`` returns True for stages with ``saga`` set, False otherwise."""
    runtime = QuadroSagaRuntime()
    saga = SagaAlias("t").deterministic("a", lambda ctx: 1).build()

    saga_spec = StageSpec(capability="x", saga=saga)
    workflow_spec = StageSpec(capability="y", workflow=object())
    bare_spec = StageSpec(capability="z")

    assert runtime.can_handle(saga_spec) is True
    assert runtime.can_handle(workflow_spec) is False
    assert runtime.can_handle(bare_spec) is False


def test_runtime_runs_simple_two_step_saga() -> None:
    """A saga with two deterministic steps runs to completion and
    produces the last step's output as the stage result."""
    saga = (
        SagaAlias("test")
        .deterministic("first", lambda ctx: "result_a")
        .deterministic("second", lambda ctx: ctx.step["first"] + "_then_b")
        .build()
    )
    spec = StageSpec(capability="x", success_status="done", saga=saga)
    runtime = QuadroSagaRuntime()
    store: dict = {}

    result = asyncio.run(
        runtime.run_stage(_ctx(spec, {"task_id": "t1"}, _fake_board_fn(store)))
    )

    assert result.output == "result_a_then_b"
    assert result.status == "done"
    assert result.terminal_reason == "saga_completed"


def test_runtime_persists_state_after_each_step() -> None:
    """After running, the saga state is stored under ``_saga:{task_id}``
    and reflects all completed steps."""
    saga = (
        SagaAlias("test")
        .deterministic("a", lambda ctx: 1)
        .deterministic("b", lambda ctx: 2)
        .build()
    )
    spec = StageSpec(capability="x", saga=saga)
    runtime = QuadroSagaRuntime()
    store: dict = {}

    asyncio.run(runtime.run_stage(_ctx(spec, {"task_id": "t1"}, _fake_board_fn(store))))

    persisted = store["_saga:t1"]
    assert persisted["saga_name"] == "test"
    assert persisted["pc"] is None  # saga complete
    assert persisted["completed_steps"] == {"a": 1, "b": 2}


def test_runtime_resumes_from_persisted_pc() -> None:
    """If saga state already exists for the task, the runtime resumes
    from the persisted pc and does not re-execute completed steps."""
    counter = {"a": 0, "b": 0}

    def _bump(name: str):
        def _impl(ctx):
            counter[name] += 1
            return name

        return _impl

    saga = (
        SagaAlias("test")
        .deterministic("a", _bump("a"))
        .deterministic("b", _bump("b"))
        .build()
    )
    spec = StageSpec(capability="x", saga=saga)
    runtime = QuadroSagaRuntime()

    # Pre-populate the store with a state that says "step a is done, pc=b".
    store = {
        "_saga:t1": {
            "saga_name": "test",
            "pc": "b",
            "idempotency_key": None,
            "completed_steps": {"a": "a"},
            "evidence": {},
            "stamps": [],
            "fork_children": {},
            "waiting_for": None,
            "started_at": None,
            "sla_deadline": None,
        }
    }
    asyncio.run(runtime.run_stage(_ctx(spec, {"task_id": "t1"}, _fake_board_fn(store))))

    # Step a must NOT have run again. Step b must have run exactly once.
    assert counter == {"a": 0, "b": 1}


def test_runtime_async_step_is_awaited() -> None:
    """A coroutine-function step is awaited; its return value is captured."""

    async def _async_step(ctx):
        return "async_result"

    saga = SagaAlias("test").deterministic("a", _async_step).build()
    spec = StageSpec(capability="x", success_status="done", saga=saga)
    runtime = QuadroSagaRuntime()
    store: dict = {}

    result = asyncio.run(
        runtime.run_stage(_ctx(spec, {"task_id": "t1"}, _fake_board_fn(store)))
    )
    assert result.output == "async_result"


def test_runtime_resumes_correctly_after_a_gate_has_routed() -> None:
    """If a gate has already evaluated and routed in a previous worker
    invocation (state.pc points at the routed-to step, completed_steps
    contains {gate_name: {"chosen": ...}}), the runtime resumes into
    the routed branch without re-evaluating the gate.

    The saga's analogue to ``test_runtime_resumes_from_persisted_pc``
    above, but for the gate-driven case where ``pc`` was advanced by
    routing rather than by linear declaration order. This is the
    load-bearing re-entrance property that milestone C inherits from
    milestone A and must preserve for every new step kind.
    """
    when_called = {"count": 0}

    def _wrapped_when(ctx):
        when_called["count"] += 1
        return True

    saga = (
        SagaAlias("test")
        .gate(
            "decision",
            when=_wrapped_when,
            on_true="left",
            on_false="right",
        )
        .deterministic("left", lambda ctx: "L")
        .deterministic("right", lambda ctx: "R")
        .build()
    )

    runtime = QuadroSagaRuntime()
    spec = StageSpec(capability="x", saga=saga)

    # Pre-populate the store as if the gate has already evaluated to
    # True in a previous invocation that crashed before "left" ran.
    store = {
        "_saga:t1": {
            "saga_name": "test",
            "pc": "left",
            "idempotency_key": None,
            "completed_steps": {"decision": {"chosen": "left"}},
            "evidence": {},
            "stamps": [],
            "fork_children": {},
            "waiting_for": None,
            "started_at": None,
            "sla_deadline": None,
        }
    }
    asyncio.run(runtime.run_stage(_ctx(spec, {"task_id": "t1"}, _fake_board_fn(store))))

    # The gate's predicate was NOT re-invoked.
    assert when_called["count"] == 0


def test_runtime_emits_telemetry_per_step() -> None:
    """Each step produces saga.step_start and saga.step_end events; the
    saga itself produces saga.start and saga.complete bookends."""
    saga = (
        SagaAlias("test")
        .deterministic("a", lambda ctx: 1)
        .deterministic("b", lambda ctx: 2)
        .build()
    )
    spec = StageSpec(capability="x", saga=saga)
    runtime = QuadroSagaRuntime()
    store: dict = {}

    result = asyncio.run(
        runtime.run_stage(_ctx(spec, {"task_id": "t1"}, _fake_board_fn(store)))
    )

    event_types = [e["event_type"] for e in result.telemetry]
    assert event_types[0] == "saga.start"
    assert "saga.step_start" in event_types
    assert "saga.step_end" in event_types
    assert event_types[-1] == "saga.complete"
    # All events must carry the runtime_id and the schema version.
    for e in result.telemetry:
        assert e["runtime"] == "quadro_saga"
        assert e["schema_version"] == "quadro.runtime_event.v1"

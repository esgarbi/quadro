"""
Unit tests for the gate, guard, expect, evidence, and stamp step kinds.

Each step kind has a small cluster of tests that pin its behaviour:
  - happy-path dispatch and stored output
  - failure mode (validation error, wrong shape, missing config)
  - routing or persistence side-effect (where applicable)

Uses a fake board_fn (in-memory dict) and a fake reasoner. No LLM key
required.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from quadro.pipeline import StageSpec
from quadro.runtime_plugins.base import RuntimeContext
from quadro.runtime_plugins.saga import QuadroSagaRuntime
from quadro.saga import Saga


# ── Fake board_fn ──────────────────────────────────────────────────────────────


def _fake_board_fn(store: dict) -> Any:
    def _fn(intent: str, payload: dict) -> dict:
        if intent == "board.put_data":
            store[payload["key"]] = payload["value"]
            return {"ok": True}
        if intent == "board.get_data":
            return {"key": payload["key"], "value": store.get(payload["key"])}
        if intent == "board.update_task":
            store.setdefault("_updates", []).append(payload)
            return {"ok": True}
        if intent == "board.get_full_state":
            return {"tasks": store.get("_tasks") or []}
        raise AssertionError(f"unexpected intent: {intent}")
    return _fn


def _ctx(spec: StageSpec, task: dict, store: dict) -> RuntimeContext:
    return RuntimeContext(
        stage=spec,
        task=task,
        context={"payload": {"task": task}},
        board_fn=_fake_board_fn(store),
    )


# ── gate ───────────────────────────────────────────────────────────────────────


def test_gate_routes_to_on_true_branch_when_predicate_returns_true() -> None:
    """A gate's `when` predicate evaluating to True jumps `pc` to
    `on_true`; the `on_false` branch's step does not run."""
    saga = (
        Saga("test")
        .deterministic("seed", lambda ctx: {"approved": True})
        .gate(
            "decision",
            when=lambda ctx: ctx.step["seed"]["approved"],
            on_true="approve_path",
            on_false="reject_path",
        )
        .deterministic("approve_path", lambda ctx: "approved")
        .deterministic("reject_path", lambda ctx: "rejected")
        .build()
    )

    runtime = QuadroSagaRuntime()
    spec = StageSpec(capability="x", saga=saga)

    result = asyncio.run(runtime.run_stage(_ctx(spec, {"task_id": "t1"}, {})))

    # Approve path's output is the saga's final output.
    assert result.output == "approved"


def test_gate_routes_to_on_false_branch_when_predicate_returns_false() -> None:
    """Symmetric to the above — predicate returns False, on_false runs."""
    saga = (
        Saga("test")
        .deterministic("seed", lambda ctx: {"approved": False})
        .gate(
            "decision",
            when=lambda ctx: ctx.step["seed"]["approved"],
            on_true="approve_path",
            on_false="reject_path",
        )
        .deterministic("approve_path", lambda ctx: "approved")
        .deterministic("reject_path", lambda ctx: "rejected")
        .build()
    )
    runtime = QuadroSagaRuntime()
    spec = StageSpec(capability="x", saga=saga)
    result = asyncio.run(runtime.run_stage(_ctx(spec, {"task_id": "t1"}, {})))
    assert result.output == "rejected"


def test_gate_records_chosen_branch_in_completed_steps() -> None:
    """The gate's stored output is the name of the chosen branch step,
    so telemetry / audit can recover which way it went without re-running."""
    saga = (
        Saga("test")
        .gate(
            "decision",
            when=lambda ctx: True,
            on_true="left",
            on_false="right",
        )
        .deterministic("left", lambda ctx: "L")
        .deterministic("right", lambda ctx: "R")
        .build()
    )
    runtime = QuadroSagaRuntime()
    spec = StageSpec(capability="x", saga=saga)
    store: dict = {}
    asyncio.run(runtime.run_stage(_ctx(spec, {"task_id": "t1"}, store)))
    persisted = store["_saga:t1"]
    assert persisted["completed_steps"]["decision"] == {"chosen": "left"}


def test_gate_rejects_branch_target_that_does_not_exist_at_build_time() -> None:
    """A gate referencing a step name that was never declared raises
    at build() time, not at dispatch time."""
    with pytest.raises(ValueError, match="references a step that was never declared"):
        (
            Saga("test")
            .gate(
                "decision",
                when=lambda ctx: True,
                on_true="ghost",
                on_false="real",
            )
            .deterministic("real", lambda ctx: "ok")
            .build()
        )


# ── guard ──────────────────────────────────────────────────────────────────────


def test_guard_passes_when_check_returns_true() -> None:
    """A guard whose check returns True is a no-op — execution
    continues to the next step in declaration order."""
    saga = (
        Saga("test")
        .guard("must_have_id", check=lambda ctx: bool(ctx.task.get("task_id")))
        .deterministic("after", lambda ctx: "ok")
        .build()
    )
    runtime = QuadroSagaRuntime()
    spec = StageSpec(capability="x", saga=saga)
    result = asyncio.run(runtime.run_stage(_ctx(spec, {"task_id": "t1"}, {})))
    assert result.output == "ok"


def test_guard_fails_saga_with_terminal_reason_when_check_returns_false() -> None:
    """A guard whose check returns False fails the saga immediately.
    The StageRunResult's terminal_reason names the guard."""
    saga = (
        Saga("test")
        .guard("must_have_id", check=lambda ctx: False)
        .deterministic("after", lambda ctx: "ok")
        .build()
    )
    runtime = QuadroSagaRuntime()
    spec = StageSpec(capability="x", saga=saga, failure_status="failed")
    result = asyncio.run(runtime.run_stage(_ctx(spec, {"task_id": "t1"}, {})))
    assert result.terminal_reason == "guard_failed:must_have_id"
    assert result.status == "failed"


def test_guard_failure_does_not_run_subsequent_steps() -> None:
    """When a guard fails, no later step runs — verified by checking
    that a subsequent deterministic step's side effect did not occur."""
    side_effect: list[str] = []
    saga = (
        Saga("test")
        .guard("blocker", check=lambda ctx: False)
        .deterministic("never_runs", lambda ctx: side_effect.append("ran"))
        .build()
    )
    runtime = QuadroSagaRuntime()
    spec = StageSpec(capability="x", saga=saga, failure_status="failed")
    asyncio.run(runtime.run_stage(_ctx(spec, {"task_id": "t1"}, {})))
    assert side_effect == []


# ── expect ─────────────────────────────────────────────────────────────────────


def test_expect_passes_when_invariant_holds() -> None:
    """An expect step whose invariant returns True is a no-op."""
    saga = (
        Saga("test")
        .deterministic("seed", lambda ctx: {"value": 42})
        .expect("value_is_positive", invariant=lambda ctx: ctx.step["seed"]["value"] > 0)
        .deterministic("after", lambda ctx: "ok")
        .build()
    )
    runtime = QuadroSagaRuntime()
    spec = StageSpec(capability="x", saga=saga)
    result = asyncio.run(runtime.run_stage(_ctx(spec, {"task_id": "t1"}, {})))
    assert result.output == "ok"


def test_expect_fails_saga_when_invariant_returns_false() -> None:
    """An expect step whose invariant returns False fails the saga.
    The terminal_reason names the expect step."""
    saga = (
        Saga("test")
        .deterministic("seed", lambda ctx: {"value": -1})
        .expect("value_is_positive", invariant=lambda ctx: ctx.step["seed"]["value"] > 0)
        .build()
    )
    runtime = QuadroSagaRuntime()
    spec = StageSpec(capability="x", saga=saga, failure_status="failed")
    result = asyncio.run(runtime.run_stage(_ctx(spec, {"task_id": "t1"}, {})))
    assert result.terminal_reason == "expect_failed:value_is_positive"


def test_expect_distinguishes_from_guard_in_telemetry() -> None:
    """Both guard and expect halt the saga on failure, but their
    telemetry events use distinct event_type values so audit queries
    can tell them apart."""
    saga_guard = Saga("g").guard("x", check=lambda ctx: False).build()
    saga_expect = Saga("e").expect("y", invariant=lambda ctx: False).build()
    runtime = QuadroSagaRuntime()

    r1 = asyncio.run(runtime.run_stage(_ctx(
        StageSpec(capability="a", saga=saga_guard, failure_status="f"),
        {"task_id": "t1"},
        {},
    )))
    r2 = asyncio.run(runtime.run_stage(_ctx(
        StageSpec(capability="b", saga=saga_expect, failure_status="f"),
        {"task_id": "t2"},
        {},
    )))

    g_events = {e["event_type"] for e in r1.telemetry}
    e_events = {e["event_type"] for e in r2.telemetry}
    assert "saga.guard_failed" in g_events
    assert "saga.expect_failed" in e_events


# ── evidence ───────────────────────────────────────────────────────────────────


def test_evidence_step_records_into_state_evidence() -> None:
    """An evidence step's `capture` callable produces a dict that is
    merged into state.evidence under the step name."""
    saga = (
        Saga("test")
        .deterministic("seed", lambda ctx: "data")
        .evidence(
            "audit",
            capture=lambda ctx: {"seen_value": ctx.step["seed"], "user": "alice"},
        )
        .deterministic("after", lambda ctx: ctx.evidence["audit"]["user"])
        .build()
    )
    runtime = QuadroSagaRuntime()
    spec = StageSpec(capability="x", saga=saga)
    store: dict = {}
    result = asyncio.run(runtime.run_stage(_ctx(spec, {"task_id": "t1"}, store)))
    assert result.output == "alice"
    persisted = store["_saga:t1"]
    assert persisted["evidence"]["audit"] == {"seen_value": "data", "user": "alice"}


def test_evidence_step_capture_failure_is_logged_but_does_not_fail_saga() -> None:
    """A capture callable raising is recorded as a warning; the saga
    proceeds to the next step. Evidence capture never fails a saga."""
    def _broken(ctx):
        raise RuntimeError("evidence capture exploded")

    saga = (
        Saga("test")
        .evidence("flaky", capture=_broken)
        .deterministic("after", lambda ctx: "still ran")
        .build()
    )
    runtime = QuadroSagaRuntime()
    spec = StageSpec(capability="x", saga=saga)
    result = asyncio.run(runtime.run_stage(_ctx(spec, {"task_id": "t1"}, {})))
    assert result.output == "still ran"


# ── stamp ──────────────────────────────────────────────────────────────────────


def test_stamp_step_records_signed_record_into_state_stamps() -> None:
    """A stamp step appends a record to state.stamps with the configured
    key, the captured value, and a UTC timestamp."""
    saga = (
        Saga("test")
        .deterministic("seed", lambda ctx: {"version": "1.2.3"})
        .stamp("release_marker", capture=lambda ctx: ctx.step["seed"]["version"])
        .build()
    )
    runtime = QuadroSagaRuntime()
    spec = StageSpec(capability="x", saga=saga)
    store: dict = {}
    asyncio.run(runtime.run_stage(_ctx(spec, {"task_id": "t1"}, store)))
    persisted = store["_saga:t1"]
    assert len(persisted["stamps"]) == 1
    stamp = persisted["stamps"][0]
    assert stamp["key"] == "release_marker"
    assert stamp["value"] == "1.2.3"
    assert "timestamp" in stamp


def test_stamp_step_supports_multiple_stamps_in_declaration_order() -> None:
    """Multiple stamps in a single saga produce an ordered list."""
    saga = (
        Saga("test")
        .stamp("s1", capture=lambda ctx: "first")
        .stamp("s2", capture=lambda ctx: "second")
        .stamp("s3", capture=lambda ctx: "third")
        .build()
    )
    runtime = QuadroSagaRuntime()
    spec = StageSpec(capability="x", saga=saga)
    store: dict = {}
    asyncio.run(runtime.run_stage(_ctx(spec, {"task_id": "t1"}, store)))
    persisted = store["_saga:t1"]
    assert [s["key"] for s in persisted["stamps"]] == ["s1", "s2", "s3"]


# ── builder validation ────────────────────────────────────────────────────────


def test_builder_rejects_gate_with_non_callable_when() -> None:
    """The .gate() builder validates `when` is callable at build time."""
    with pytest.raises(TypeError, match="when must be callable"):
        Saga("test").gate("g", when="not callable", on_true="x", on_false="y")


def test_builder_rejects_guard_with_non_callable_check() -> None:
    """The .guard() builder validates `check` is callable at build time."""
    with pytest.raises(TypeError, match="check must be callable"):
        Saga("test").guard("g", check=42)


def test_builder_rejects_expect_with_non_callable_invariant() -> None:
    """The .expect() builder validates `invariant` is callable at build time."""
    with pytest.raises(TypeError, match="invariant must be callable"):
        Saga("test").expect("e", invariant=None)

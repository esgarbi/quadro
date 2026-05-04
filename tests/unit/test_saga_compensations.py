"""
Unit tests for saga compensation rollback (milestone D).

Compensations are registered via ``.compensate(step_name, undo=fn)`` on
the builder and stored on ``BuiltSaga.compensations``. When a step raises
mid-saga, the runtime walks the completed steps in reverse order and
invokes each registered ``undo`` callable. ``state.compensations_run`` is
an ordered list of attempt records — one per invoked compensation, with
``{step, outcome, duration_ms, timestamp}`` (plus ``error`` for failures).

Two failure-handling modes:
  - ``on_failure="continue"`` (default) — a compensation that itself
    raises is logged in ``compensations_run`` and the walker proceeds
    to the next earlier compensation.
  - ``on_failure="halt"`` — a raising compensation aborts the walk.
    Earlier compensations are NOT invoked.

Three terminal reasons summarise the outcome of the rollback walk:
  - ``"compensated:<original_step>"`` — every compensation succeeded.
  - ``"compensation_partial:<original_step>"`` — at least one
    compensation failed under continue-mode but the walker ran through
    the rest.
  - ``"compensation_failed:<step>"`` — a halt-mode compensation raised
    and the walker stopped.

Uses the same fake-board pattern the step-kind and modifier tests
established. No LLM key required.
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


# ── Walking order ────────────────────────────────────────────────────────────


def test_compensation_walks_in_reverse_completion_order() -> None:
    """A saga A→B→C where C raises invokes compensations in order
    B-comp then A-comp. C never completed, so C has no compensation to
    invoke. The order is completion-reverse, not declaration-reverse."""
    order: list[str] = []

    def _raise(_ctx):
        raise RuntimeError("boom")

    saga = (
        Saga("test")
        .deterministic("a", lambda ctx: "a_ok")
        .compensate("a", undo=lambda ctx: order.append("a_undo"))
        .deterministic("b", lambda ctx: "b_ok")
        .compensate("b", undo=lambda ctx: order.append("b_undo"))
        .deterministic("c", _raise)
        .compensate("c", undo=lambda ctx: order.append("c_undo"))
        .build()
    )

    runtime = QuadroSagaRuntime()
    spec = StageSpec(capability="x", saga=saga, failure_status="failed")
    store: dict = {}
    result = asyncio.run(runtime.run_stage(_ctx(spec, {"task_id": "t1"}, store)))

    assert order == ["b_undo", "a_undo"]
    assert result.terminal_reason == "compensated:c"


def test_compensation_skips_steps_without_registered_undo() -> None:
    """A saga A→B→C where B has no .compensate(...) and C raises: the
    walker invokes only A-comp. B is silently skipped (no error,
    no recorded attempt) because it has no registered undo."""
    order: list[str] = []

    def _raise(_ctx):
        raise RuntimeError("boom")

    saga = (
        Saga("test")
        .deterministic("a", lambda ctx: "a_ok")
        .compensate("a", undo=lambda ctx: order.append("a_undo"))
        .deterministic("b", lambda ctx: "b_ok")
        # no .compensate("b", ...) — intentionally skipped
        .deterministic("c", _raise)
        .build()
    )

    runtime = QuadroSagaRuntime()
    spec = StageSpec(capability="x", saga=saga, failure_status="failed")
    store: dict = {}
    asyncio.run(runtime.run_stage(_ctx(spec, {"task_id": "t1"}, store)))

    assert order == ["a_undo"]
    persisted = store["_saga:t1"]
    # compensations_run records only invoked compensations — not the
    # silently-skipped B.
    attempted = [r["step"] for r in persisted["compensations_run"]]
    assert attempted == ["a"]


def test_compensation_records_every_attempt_in_state() -> None:
    """state.compensations_run is an ordered list of dicts. Each dict has
    at minimum ``step``, ``outcome`` ("ok"|"failed"), ``duration_ms``,
    and ``timestamp``. Only invoked compensations are recorded; steps
    that had no registered undo (or never completed) are not."""

    def _raise(_ctx):
        raise RuntimeError("boom at c")

    saga = (
        Saga("test")
        .deterministic("a", lambda ctx: "a_ok")
        .compensate("a", undo=lambda ctx: None)
        .deterministic("b", lambda ctx: "b_ok")
        .compensate("b", undo=lambda ctx: None)
        .deterministic("c", _raise)
        .build()
    )

    runtime = QuadroSagaRuntime()
    spec = StageSpec(capability="x", saga=saga, failure_status="failed")
    store: dict = {}
    asyncio.run(runtime.run_stage(_ctx(spec, {"task_id": "t1"}, store)))

    persisted = store["_saga:t1"]
    runs = persisted["compensations_run"]
    assert [r["step"] for r in runs] == ["b", "a"]
    for r in runs:
        assert r["outcome"] == "ok"
        assert "duration_ms" in r
        assert "timestamp" in r


# ── Halt vs continue semantics ────────────────────────────────────────────────


def test_compensation_continues_on_inner_failure_by_default() -> None:
    """When a compensation itself raises under the default
    ``on_failure="continue"`` setting, the walker logs the failure and
    proceeds with the next earlier compensation. Option 2 semantics."""
    order: list[str] = []

    def _raise_main(_ctx):
        raise RuntimeError("boom at c")

    def _undo_b_fails(_ctx):
        order.append("b_undo_attempted")
        raise ValueError("b compensation exploded")

    saga = (
        Saga("test")
        .deterministic("a", lambda ctx: "a_ok")
        .compensate("a", undo=lambda ctx: order.append("a_undo"))
        .deterministic("b", lambda ctx: "b_ok")
        .compensate("b", undo=_undo_b_fails)
        .deterministic("c", _raise_main)
        .build()
    )

    runtime = QuadroSagaRuntime()
    spec = StageSpec(capability="x", saga=saga, failure_status="failed")
    store: dict = {}
    result = asyncio.run(runtime.run_stage(_ctx(spec, {"task_id": "t1"}, store)))

    # Both compensations were attempted, in reverse completion order.
    assert order == ["b_undo_attempted", "a_undo"]
    # terminal_reason is "partial" because one compensation failed.
    assert result.terminal_reason == "compensation_partial:c"


def test_compensation_halts_when_step_opted_into_halt_mode() -> None:
    """A compensation declared with ``on_failure="halt"`` that raises
    aborts the rollback walk. Earlier compensations are NOT invoked.
    terminal_reason names the compensation that failed, not the
    original failing step."""
    order: list[str] = []

    def _raise_main(_ctx):
        raise RuntimeError("boom at c")

    def _undo_b_fails(_ctx):
        order.append("b_undo_attempted")
        raise ValueError("b compensation exploded")

    saga = (
        Saga("test")
        .deterministic("a", lambda ctx: "a_ok")
        .compensate("a", undo=lambda ctx: order.append("a_undo"))
        .deterministic("b", lambda ctx: "b_ok")
        .compensate("b", undo=_undo_b_fails, on_failure="halt")
        .deterministic("c", _raise_main)
        .build()
    )

    runtime = QuadroSagaRuntime()
    spec = StageSpec(capability="x", saga=saga, failure_status="failed")
    store: dict = {}
    result = asyncio.run(runtime.run_stage(_ctx(spec, {"task_id": "t1"}, store)))

    # Only b's compensation was attempted; a's was never reached.
    assert order == ["b_undo_attempted"]
    assert result.terminal_reason == "compensation_failed:b"


def test_compensation_partial_completion_records_terminal_reason() -> None:
    """A rollback walk where one compensation fails under
    ``on_failure="continue"`` produces the partial terminal_reason,
    distinguishing it from clean rollback and halt modes."""

    def _raise_main(_ctx):
        raise RuntimeError("boom at b")

    def _undo_a_fails(_ctx):
        raise ValueError("a compensation also broken")

    saga = (
        Saga("test")
        .deterministic("a", lambda ctx: "a_ok")
        .compensate("a", undo=_undo_a_fails)
        .deterministic("b", _raise_main)
        .build()
    )

    runtime = QuadroSagaRuntime()
    spec = StageSpec(capability="x", saga=saga, failure_status="failed")
    store: dict = {}
    result = asyncio.run(runtime.run_stage(_ctx(spec, {"task_id": "t1"}, store)))

    assert result.terminal_reason == "compensation_partial:b"
    persisted = store["_saga:t1"]
    assert persisted["compensations_run"][0]["outcome"] == "failed"


# ── Telemetry ─────────────────────────────────────────────────────────────────


def test_compensation_emits_start_end_events_per_step() -> None:
    """Each compensation invocation produces a saga.compensation_start
    and saga.compensation_end event, carrying the step name."""

    def _raise(_ctx):
        raise RuntimeError("boom at c")

    saga = (
        Saga("test")
        .deterministic("a", lambda ctx: "a_ok")
        .compensate("a", undo=lambda ctx: None)
        .deterministic("b", lambda ctx: "b_ok")
        .compensate("b", undo=lambda ctx: None)
        .deterministic("c", _raise)
        .build()
    )

    runtime = QuadroSagaRuntime()
    spec = StageSpec(capability="x", saga=saga, failure_status="failed")
    result = asyncio.run(runtime.run_stage(_ctx(spec, {"task_id": "t1"}, {})))

    types = [e["event_type"] for e in result.telemetry]
    assert types.count("saga.compensation_start") == 2
    assert types.count("saga.compensation_end") == 2
    # Each start should be followed by its matching end before the next start.
    starts = [e for e in result.telemetry if e["event_type"] == "saga.compensation_start"]
    assert [e["step_name"] for e in starts] == ["b", "a"]


def test_compensation_emits_started_completed_alias_events() -> None:
    def _raise(_ctx):
        raise RuntimeError("boom at b")

    saga = (
        Saga("test")
        .deterministic("a", lambda ctx: "a_ok")
        .compensate("a", undo=lambda ctx: None)
        .deterministic("b", _raise)
        .build()
    )

    runtime = QuadroSagaRuntime()
    spec = StageSpec(capability="x", saga=saga, failure_status="failed")
    result = asyncio.run(runtime.run_stage(_ctx(spec, {"task_id": "t1"}, {})))

    started = [
        e for e in result.telemetry
        if e["event_type"] == "saga.compensation_started"
    ]
    completed = [
        e for e in result.telemetry
        if e["event_type"] == "saga.compensation_completed"
    ]
    assert len(started) == 1
    assert len(completed) == 1
    assert started[0]["payload"] == {"step": "a", "attempt_number": 1}
    assert completed[0]["payload"]["step"] == "a"
    assert isinstance(completed[0]["payload"]["duration_ms"], int)


def test_compensation_emits_failed_event_on_inner_raise() -> None:
    """A compensation that raises produces a saga.compensation_failed
    event with the exception type in payload."""

    def _raise_main(_ctx):
        raise RuntimeError("boom at b")

    def _undo_a_fails(_ctx):
        raise ValueError("a compensation exploded")

    saga = (
        Saga("test")
        .deterministic("a", lambda ctx: "a_ok")
        .compensate("a", undo=_undo_a_fails)
        .deterministic("b", _raise_main)
        .build()
    )

    runtime = QuadroSagaRuntime()
    spec = StageSpec(capability="x", saga=saga, failure_status="failed")
    result = asyncio.run(runtime.run_stage(_ctx(spec, {"task_id": "t1"}, {})))

    failed_events = [
        e for e in result.telemetry
        if e["event_type"] == "saga.compensation_failed"
    ]
    assert len(failed_events) == 1
    payload = failed_events[0]["payload"]
    assert payload["step"] == "a"
    assert payload["error_type"] == "ValueError"


def test_compensation_emits_rollback_complete_at_end() -> None:
    """After the walk finishes (either cleanly or partially), exactly
    one saga.rollback_complete event summarises the run."""

    def _raise(_ctx):
        raise RuntimeError("boom at b")

    saga = (
        Saga("test")
        .deterministic("a", lambda ctx: "a_ok")
        .compensate("a", undo=lambda ctx: None)
        .deterministic("b", _raise)
        .build()
    )

    runtime = QuadroSagaRuntime()
    spec = StageSpec(capability="x", saga=saga, failure_status="failed")
    result = asyncio.run(runtime.run_stage(_ctx(spec, {"task_id": "t1"}, {})))

    complete_events = [
        e for e in result.telemetry
        if e["event_type"] == "saga.rollback_complete"
    ]
    assert len(complete_events) == 1
    payload = complete_events[0]["payload"]
    assert payload["compensations_invoked"] == 1
    assert payload["compensations_failed"] == 0


# ── Resume after crash mid-rollback ──────────────────────────────────────────


def test_runtime_resumes_compensation_walk_from_partial_state() -> None:
    """A saga state with compensations_run partially populated (one
    compensation logged as ok, one still pending) resumes by invoking
    only the un-compensated compensations on the next run_stage call."""
    calls: list[str] = []

    def _raise(_ctx):
        # If the runtime re-enters the failed step, it should raise
        # again and re-trigger compensation. The pre-populated state
        # below simulates "c failed; b's compensation already ran; a
        # still pending."
        raise RuntimeError("boom at c")

    saga = (
        Saga("test")
        .deterministic("a", lambda ctx: "a_ok")
        .compensate("a", undo=lambda ctx: calls.append("a_undo"))
        .deterministic("b", lambda ctx: "b_ok")
        .compensate("b", undo=lambda ctx: calls.append("b_undo"))
        .deterministic("c", _raise)
        .build()
    )

    runtime = QuadroSagaRuntime()
    spec = StageSpec(capability="x", saga=saga, failure_status="failed")

    # Pre-populated state: c failed; b already compensated cleanly; a
    # not yet compensated. pc points at c (the failed step).
    store = {
        "_saga:t1": {
            "saga_name": "test",
            "pc": "c",
            "idempotency_key": None,
            "completed_steps": {"a": "a_ok", "b": "b_ok"},
            "evidence": {},
            "stamps": [],
            "fork_children": {},
            "waiting_for": None,
            "started_at": None,
            "sla_deadline": None,
            "compensations_run": [
                {
                    "step": "b",
                    "outcome": "ok",
                    "duration_ms": 1,
                    "timestamp": "2026-01-01T00:00:00+00:00",
                }
            ],
        }
    }
    asyncio.run(runtime.run_stage(_ctx(spec, {"task_id": "t1"}, store)))

    # Only a's compensation was invoked on resume; b was not re-run.
    assert calls == ["a_undo"]


def test_runtime_re_invokes_in_flight_compensation_on_resume() -> None:
    """A compensation with no terminal record (no matching
    compensations_run entry) is re-attempted on resume. Idempotency is
    the author's responsibility; the runtime does not deduplicate."""
    calls: list[str] = []

    def _raise(_ctx):
        raise RuntimeError("boom at b")

    saga = (
        Saga("test")
        .deterministic("a", lambda ctx: "a_ok")
        .compensate("a", undo=lambda ctx: calls.append("a_undo"))
        .deterministic("b", _raise)
        .build()
    )

    runtime = QuadroSagaRuntime()
    spec = StageSpec(capability="x", saga=saga, failure_status="failed")

    # State mid-rollback but no compensation recorded yet — the worker
    # crashed before ``_apply_compensations`` could write.
    store = {
        "_saga:t1": {
            "saga_name": "test",
            "pc": "b",
            "idempotency_key": None,
            "completed_steps": {"a": "a_ok"},
            "evidence": {},
            "stamps": [],
            "fork_children": {},
            "waiting_for": None,
            "started_at": None,
            "sla_deadline": None,
            "compensations_run": [],
        }
    }
    asyncio.run(runtime.run_stage(_ctx(spec, {"task_id": "t1"}, store)))

    # a's compensation fires because its record is not in compensations_run.
    assert calls == ["a_undo"]


# ── Idempotency contract ──────────────────────────────────────────────────────


def test_compensation_function_is_called_once_on_clean_rollback() -> None:
    """In the absence of crashes, each registered compensation is
    invoked exactly once."""
    counter = {"a": 0, "b": 0}

    def _raise(_ctx):
        raise RuntimeError("boom at c")

    saga = (
        Saga("test")
        .deterministic("a", lambda ctx: "a_ok")
        .compensate("a", undo=lambda ctx: counter.__setitem__("a", counter["a"] + 1))
        .deterministic("b", lambda ctx: "b_ok")
        .compensate("b", undo=lambda ctx: counter.__setitem__("b", counter["b"] + 1))
        .deterministic("c", _raise)
        .build()
    )

    runtime = QuadroSagaRuntime()
    spec = StageSpec(capability="x", saga=saga, failure_status="failed")
    asyncio.run(runtime.run_stage(_ctx(spec, {"task_id": "t1"}, {})))

    assert counter == {"a": 1, "b": 1}


def test_compensation_runs_against_completed_steps_outputs() -> None:
    """The SagaContext passed to a compensation includes the output of
    the corresponding completed step in ``ctx.step[<name>]``, so the
    compensation can read what was done and undo it precisely."""
    observed: dict = {}

    def _undo_reserve(sctx):
        # Read the reservation record produced by the forward step.
        observed.update(sctx.step["reserve_inventory"])

    def _raise(_ctx):
        raise RuntimeError("boom at ship")

    saga = (
        Saga("test")
        .deterministic("reserve_inventory",
                       lambda ctx: {"sku": "SKU-A", "qty": 3, "from": "WH-MAIN"})
        .compensate("reserve_inventory", undo=_undo_reserve)
        .deterministic("ship", _raise)
        .build()
    )

    runtime = QuadroSagaRuntime()
    spec = StageSpec(capability="x", saga=saga, failure_status="failed")
    asyncio.run(runtime.run_stage(_ctx(spec, {"task_id": "t1"}, {})))

    assert observed == {"sku": "SKU-A", "qty": 3, "from": "WH-MAIN"}


# ── Builder validation ───────────────────────────────────────────────────────


def test_builder_rejects_compensate_for_undeclared_step_at_build() -> None:
    """Existing milestone-A check, retained: ``build()`` raises if
    ``.compensate('ghost', undo=fn)`` references a step that was never
    added. Milestone D's addition of the ``on_failure`` parameter does
    not change this validation."""
    with pytest.raises(ValueError, match="references a step that was never declared"):
        (
            Saga("test")
            .deterministic("real", lambda ctx: "ok")
            .compensate("ghost", undo=lambda ctx: None)
            .build()
        )

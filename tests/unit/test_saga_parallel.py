"""
Unit tests for ``Saga.parallel(...)`` step kind.

Covers all three join modes (``all``, ``any``, ``n_of_m``),
compensation walking through completed parallel steps, cancellation
semantics for ``any`` and ``n_of_m``, and resume semantics after a worker
crash mid-parallel.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from quadro.pipeline import StageSpec
from quadro.runtime_plugins.base import RuntimeContext
from quadro.runtime_plugins.saga import QuadroSagaRuntime
from quadro.saga import Saga


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


def _run(saga, *, task_id: str = "t1", store: dict | None = None):
    runtime = QuadroSagaRuntime()
    spec = StageSpec(capability="x", saga=saga, failure_status="failed")
    actual_store = {} if store is None else store
    result = asyncio.run(runtime.run_stage(_ctx(spec, {"task_id": task_id}, actual_store)))
    return result, actual_store


# ── join="all" ───────────────────────────────────────────────────────────────


def test_all_three_branches_succeed_outputs_dict_carries_each_branch_output() -> None:
    saga = (
        Saga("test")
        .parallel(
            "fanout",
            branches=[
                lambda b: b.deterministic("a", lambda ctx: "a_out"),
                lambda b: b.deterministic("b", lambda ctx: "b_out"),
                lambda b: b.deterministic("c", lambda ctx: "c_out"),
            ],
        )
        .build()
    )

    result, store = _run(saga)

    assert result.output == {"a": "a_out", "b": "b_out", "c": "c_out"}
    assert store["_saga:t1"]["completed_steps"]["fanout"] == {
        "a": "a_out",
        "b": "b_out",
        "c": "c_out",
    }


def test_all_one_branch_fails_parallel_step_fails_with_branch_diagnostic() -> None:
    def _raise(_ctx):
        raise RuntimeError("branch b exploded")

    saga = (
        Saga("test")
        .parallel(
            "fanout",
            branches=[
                lambda b: b.deterministic("a", lambda ctx: "a_out"),
                lambda b: b.deterministic("b", _raise),
                lambda b: b.deterministic("c", lambda ctx: "c_out"),
            ],
        )
        .build()
    )

    result, _store = _run(saga)

    assert result.status == "failed"
    assert result.terminal_reason == "step_failed:fanout"
    failed = [e for e in result.telemetry if e["event_type"] == "saga.step_failed"]
    assert failed
    assert failed[-1]["step_name"] == "fanout"
    assert "Parallel" in failed[-1]["payload"]["error_type"]
    branch_failed = [
        e for e in result.telemetry
        if e["event_type"] == "saga.parallel_branch_failed"
    ]
    assert len(branch_failed) == 1
    assert branch_failed[0]["payload"]["branch_name"] == "b"
    assert branch_failed[0]["payload"]["error_type"] == "RuntimeError"


def test_all_branch_outputs_accessible_via_nested_dict_in_later_step() -> None:
    saga = (
        Saga("test")
        .parallel(
            "fanout",
            join="all",
            branches=[
                lambda b: b.deterministic("left", lambda ctx: 10),
                lambda b: b.deterministic("right", lambda ctx: 32),
            ],
        )
        .deterministic("merge", lambda ctx: ctx.step["fanout"]["left"] + ctx.step["fanout"]["right"])
        .build()
    )

    result, _store = _run(saga)

    assert result.output == 42


def test_all_emits_parallel_branch_start_and_completion_events() -> None:
    saga = (
        Saga("test")
        .parallel(
            "fanout",
            join="all",
            branches=[
                lambda b: b.deterministic("left", lambda ctx: "L"),
                lambda b: b.deterministic("right", lambda ctx: "R"),
            ],
        )
        .build()
    )

    result, _store = _run(saga)

    branch_events = [
        e for e in result.telemetry
        if e["event_type"].startswith("saga.parallel_branch_")
    ]
    assert [e["event_type"] for e in branch_events] == [
        "saga.parallel_branch_started",
        "saga.parallel_branch_completed",
        "saga.parallel_branch_started",
        "saga.parallel_branch_completed",
    ]
    assert [e["payload"]["branch_name"] for e in branch_events] == [
        "left",
        "left",
        "right",
        "right",
    ]
    assert all(e["payload"]["parallel_step_name"] == "fanout" for e in branch_events)
    completed = [
        e for e in branch_events
        if e["event_type"] == "saga.parallel_branch_completed"
    ]
    assert all(isinstance(e["duration_ms"], int) for e in completed)


def test_all_compensation_walks_completed_branches_in_reverse_insertion_order() -> None:
    order: list[str] = []

    def _raise(_ctx):
        raise RuntimeError("after parallel")

    saga = (
        Saga("test")
        .parallel(
            "fanout",
            branches=[
                lambda b: (
                    b.deterministic("a1", lambda ctx: "a1")
                    .compensate("a1", undo=lambda ctx: order.append("a1_undo"))
                    .deterministic("a2", lambda ctx: "a2")
                    .compensate("a2", undo=lambda ctx: order.append("a2_undo"))
                ),
                lambda b: (
                    b.deterministic("b1", lambda ctx: "b1")
                    .compensate("b1", undo=lambda ctx: order.append("b1_undo"))
                    .deterministic("b2", lambda ctx: "b2")
                    .compensate("b2", undo=lambda ctx: order.append("b2_undo"))
                ),
            ],
        )
        .deterministic("after", _raise)
        .build()
    )

    result, store = _run(saga)

    assert result.terminal_reason == "compensated:after"
    assert order == ["b2_undo", "b1_undo", "a2_undo", "a1_undo"]
    assert [r["step"] for r in store["_saga:t1"]["compensations_run"]] == [
        "fanout.b1.b2",
        "fanout.b1.b1",
        "fanout.a1.a2",
        "fanout.a1.a1",
    ]


def test_all_resume_after_partial_completion_re_invokes_only_pending_branches() -> None:
    calls: list[str] = []

    saga = (
        Saga("test")
        .parallel(
            "fanout",
            branches=[
                lambda b: b.deterministic("a", lambda ctx: calls.append("a") or "a_out"),
                lambda b: b.deterministic("b", lambda ctx: calls.append("b") or "b_out"),
            ],
        )
        .build()
    )
    store = {
        "_saga:t1": {
            "saga_name": "test",
            "pc": "fanout",
            "idempotency_key": None,
            "completed_steps": {},
            "evidence": {},
            "stamps": [],
            "fork_children": {},
            "waiting_for": None,
            "started_at": "2026-01-01T00:00:00+00:00",
            "sla_deadline": None,
            "compensations_run": [],
            "reasoners_by_step": {},
            "branch_states": {
                "fanout": {
                    "a": {
                        "saga_name": "test.fanout.a",
                        "pc": None,
                        "idempotency_key": None,
                        "completed_steps": {"a": "a_out"},
                        "evidence": {},
                        "stamps": [],
                        "fork_children": {},
                        "waiting_for": None,
                        "started_at": "2026-01-01T00:00:00+00:00",
                        "sla_deadline": None,
                        "compensations_run": [],
                        "reasoners_by_step": {},
                        "branch_states": {},
                    }
                }
            },
        }
    }

    result, store = _run(saga, store=store)

    assert calls == ["b"]
    assert result.output == {"a": "a_out", "b": "b_out"}
    assert store["_saga:t1"]["branch_states"]["fanout"]["a"]["completed_steps"] == {"a": "a_out"}


# ── join="any" ───────────────────────────────────────────────────────────────


def test_any_first_branch_to_complete_wins_others_cancelled() -> None:
    events: list[str] = []

    async def _slow(_ctx):
        try:
            await asyncio.sleep(1)
            events.append("slow_done")
            return "slow"
        except asyncio.CancelledError:
            events.append("slow_cancelled")
            raise

    async def _fast(_ctx):
        await asyncio.sleep(0.01)
        events.append("fast_done")
        return "fast"

    saga = (
        Saga("test")
        .parallel(
            "fanout",
            join="any",
            branches=[
                lambda b: b.deterministic("slow", _slow),
                lambda b: b.deterministic("fast", _fast),
            ],
        )
        .build()
    )

    result, _store = _run(saga)

    assert result.output == {"fast": "fast"}
    assert events == ["fast_done", "slow_cancelled"]


def test_any_cancelled_branches_do_not_fire_compensations() -> None:
    order: list[str] = []

    async def _slow_after_side_effect(_ctx):
        await asyncio.sleep(1)
        return "slow"

    async def _fast(_ctx):
        await asyncio.sleep(0.01)
        return "fast"

    def _raise(_ctx):
        raise RuntimeError("after parallel")

    saga = (
        Saga("test")
        .parallel(
            "fanout",
            join="any",
            branches=[
                lambda b: (
                    b.deterministic("slow_pre", lambda ctx: "side_effect")
                    .compensate("slow_pre", undo=lambda ctx: order.append("slow_pre_undo"))
                    .deterministic("slow", _slow_after_side_effect)
                ),
                lambda b: (
                    b.deterministic("fast", _fast)
                    .compensate("fast", undo=lambda ctx: order.append("fast_undo"))
                ),
            ],
        )
        .deterministic("after", _raise)
        .build()
    )

    result, _store = _run(saga)

    assert result.terminal_reason == "compensated:after"
    assert order == ["fast_undo"]


def test_any_emits_cancelled_event_for_losing_branches() -> None:
    async def _slow(_ctx):
        await asyncio.sleep(1)
        return "slow"

    async def _fast(_ctx):
        await asyncio.sleep(0.01)
        return "fast"

    saga = (
        Saga("test")
        .parallel(
            "fanout",
            join="any",
            branches=[
                lambda b: b.deterministic("slow", _slow),
                lambda b: b.deterministic("fast", _fast),
            ],
        )
        .build()
    )

    result, _store = _run(saga)

    cancelled = [
        e for e in result.telemetry
        if e["event_type"] == "saga.parallel_branch_cancelled"
    ]
    assert len(cancelled) == 1
    assert cancelled[0]["payload"]["parallel_step_name"] == "fanout"
    assert cancelled[0]["payload"]["branch_name"] == "slow"


def test_any_all_branches_fail_parallel_step_fails() -> None:
    def _raise_a(_ctx):
        raise RuntimeError("a")

    def _raise_b(_ctx):
        raise RuntimeError("b")

    saga = (
        Saga("test")
        .parallel(
            "fanout",
            join="any",
            branches=[
                lambda b: b.deterministic("a", _raise_a),
                lambda b: b.deterministic("b", _raise_b),
            ],
        )
        .build()
    )

    result, _store = _run(saga)

    assert result.status == "failed"
    assert result.terminal_reason == "step_failed:fanout"


def test_any_one_succeeds_others_fail_treated_as_success() -> None:
    async def _fail_fast(_ctx):
        await asyncio.sleep(0.01)
        raise RuntimeError("not this one")

    async def _succeed_slow(_ctx):
        await asyncio.sleep(0.02)
        return "winner"

    saga = (
        Saga("test")
        .parallel(
            "fanout",
            join="any",
            branches=[
                lambda b: b.deterministic("fail", _fail_fast),
                lambda b: b.deterministic("win", _succeed_slow),
            ],
        )
        .build()
    )

    result, _store = _run(saga)

    assert result.output == {"win": "winner"}
    assert result.terminal_reason == "saga_completed"


# ── join="n_of_m" ─────────────────────────────────────────────────────────────


def test_n_of_m_threshold_met_remaining_branches_cancelled() -> None:
    events: list[str] = []

    async def _a(_ctx):
        await asyncio.sleep(0.01)
        events.append("a")
        return "a"

    async def _b(_ctx):
        await asyncio.sleep(0.02)
        events.append("b")
        return "b"

    async def _c(_ctx):
        try:
            await asyncio.sleep(1)
            events.append("c")
            return "c"
        except asyncio.CancelledError:
            events.append("c_cancelled")
            raise

    saga = (
        Saga("test")
        .parallel(
            "fanout",
            join=("n_of_m", 2),
            branches=[
                lambda b: b.deterministic("a", _a),
                lambda b: b.deterministic("b", _b),
                lambda b: b.deterministic("c", _c),
            ],
        )
        .build()
    )

    result, _store = _run(saga)

    assert result.output == {"a": "a", "b": "b"}
    assert events == ["a", "b", "c_cancelled"]


def test_n_of_m_threshold_not_met_parallel_step_fails() -> None:
    def _raise_a(_ctx):
        raise RuntimeError("a")

    def _raise_b(_ctx):
        raise RuntimeError("b")

    saga = (
        Saga("test")
        .parallel(
            "fanout",
            join=("n_of_m", 2),
            branches=[
                lambda b: b.deterministic("a", lambda ctx: "a"),
                lambda b: b.deterministic("b", _raise_a),
                lambda b: b.deterministic("c", _raise_b),
            ],
        )
        .build()
    )

    result, _store = _run(saga)

    assert result.status == "failed"
    assert result.terminal_reason == "step_failed:fanout"


def test_n_of_m_exactly_n_succeed_others_cancelled_no_compensations() -> None:
    order: list[str] = []

    async def _fast_a(_ctx):
        await asyncio.sleep(0.01)
        return "a"

    async def _fast_b(_ctx):
        await asyncio.sleep(0.02)
        return "b"

    async def _slow(_ctx):
        await asyncio.sleep(1)
        return "slow"

    def _raise(_ctx):
        raise RuntimeError("after parallel")

    saga = (
        Saga("test")
        .parallel(
            "fanout",
            join=("n_of_m", 2),
            branches=[
                lambda b: (
                    b.deterministic("a", _fast_a)
                    .compensate("a", undo=lambda ctx: order.append("a_undo"))
                ),
                lambda b: (
                    b.deterministic("b", _fast_b)
                    .compensate("b", undo=lambda ctx: order.append("b_undo"))
                ),
                lambda b: (
                    b.deterministic("c", _slow)
                    .compensate("c", undo=lambda ctx: order.append("c_undo"))
                ),
            ],
        )
        .deterministic("after", _raise)
        .build()
    )

    result, _store = _run(saga)

    assert result.terminal_reason == "compensated:after"
    assert order == ["b_undo", "a_undo"]


# ── Builder validation ───────────────────────────────────────────────────────


def test_builder_rejects_parallel_with_invalid_join_mode() -> None:
    with pytest.raises(ValueError, match="join"):
        (
            Saga("test")
            .parallel(
                "fanout",
                join="invalid",
                branches=[lambda b: b.deterministic("a", lambda ctx: "a")],
            )
            .build()
        )


def test_builder_rejects_parallel_with_empty_branches_list() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        Saga("test").parallel("fanout", branches=[]).build()


def test_builder_rejects_parallel_with_non_callable_branch_factory() -> None:
    with pytest.raises(TypeError, match="callable"):
        Saga("test").parallel("fanout", branches=[object()]).build()

"""
Unit tests for the .retry() and .deadline() saga modifiers.

Modifiers attach to the most recently added step via the builder's
_current_step pointer. They are applied by the runtime as a wrapper
around the underlying dispatch — retry catches matching exceptions
and re-runs; deadline imposes an asyncio.wait_for around the dispatch
coroutine.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from quadro.pipeline import StageSpec
from quadro.runtime_plugins.base import RuntimeContext
from quadro.runtime_plugins.saga import QuadroSagaRuntime
from quadro.saga import Saga


def _fake_board_fn(store: dict):
    def _fn(intent, payload):
        if intent == "board.put_data":
            store[payload["key"]] = payload["value"]
            return {"ok": True}
        if intent == "board.get_data":
            return {"key": payload["key"], "value": store.get(payload["key"])}
        if intent == "board.get_full_state":
            return {"tasks": []}
        raise AssertionError(intent)

    return _fn


def _ctx(spec, task, store):
    return RuntimeContext(
        stage=spec,
        task=task,
        context={"payload": {"task": task}},
        board_fn=_fake_board_fn(store),
    )


# ── retry ──────────────────────────────────────────────────────────────────────


def test_retry_re_invokes_step_on_matching_exception() -> None:
    """A step that raises an exception in `on=(...)` is retried up to
    `attempts` times. Successful retry produces the step's output as
    if no failure had occurred."""
    calls = {"count": 0}

    def _flaky(ctx):
        calls["count"] += 1
        if calls["count"] < 3:
            raise ConnectionError("transient")
        return "ok"

    saga = (
        Saga("test")
        .deterministic("flaky", _flaky)
        .retry(attempts=5, on=(ConnectionError,))
        .build()
    )
    runtime = QuadroSagaRuntime()
    spec = StageSpec(capability="x", saga=saga)
    result = asyncio.run(runtime.run_stage(_ctx(spec, {"task_id": "t1"}, {})))
    assert result.output == "ok"
    assert calls["count"] == 3


def test_retry_emits_attempt_events_before_next_try() -> None:
    calls = {"count": 0}

    def _flaky(ctx):
        calls["count"] += 1
        if calls["count"] < 3:
            raise ConnectionError("transient")
        return "ok"

    saga = (
        Saga("test")
        .deterministic("flaky", _flaky)
        .retry(attempts=3, on=(ConnectionError,))
        .build()
    )
    runtime = QuadroSagaRuntime()
    spec = StageSpec(capability="x", saga=saga)
    result = asyncio.run(runtime.run_stage(_ctx(spec, {"task_id": "t1"}, {})))

    retry_events = [
        e for e in result.telemetry if e["event_type"] == "saga.retry_attempt"
    ]
    assert [e["payload"]["attempt_number"] for e in retry_events] == [1, 2]
    assert [e["payload"]["last_error_type"] for e in retry_events] == [
        "ConnectionError",
        "ConnectionError",
    ]
    assert all(e["payload"]["sleep_seconds_before_next"] == 0.0 for e in retry_events)


def test_retry_propagates_exception_when_attempts_exhausted() -> None:
    """If the step keeps raising past `attempts`, the exception
    propagates and the saga fails."""

    def _always_fails(ctx):
        raise ConnectionError("persistent")

    saga = (
        Saga("test")
        .deterministic("doomed", _always_fails)
        .retry(attempts=2, on=(ConnectionError,))
        .build()
    )
    runtime = QuadroSagaRuntime()
    spec = StageSpec(capability="x", saga=saga, failure_status="failed")
    result = asyncio.run(runtime.run_stage(_ctx(spec, {"task_id": "t1"}, {})))
    assert result.terminal_reason.startswith("step_failed:doomed")


def test_retry_does_not_catch_non_matching_exception() -> None:
    """Retry only intercepts exceptions whose type is in `on=(...)`.
    Other exceptions bubble through immediately on first occurrence."""
    calls = {"count": 0}

    def _wrong_kind_of_failure(ctx):
        calls["count"] += 1
        raise ValueError("not in retry list")

    saga = (
        Saga("test")
        .deterministic("typed", _wrong_kind_of_failure)
        .retry(attempts=10, on=(ConnectionError,))
        .build()
    )
    runtime = QuadroSagaRuntime()
    spec = StageSpec(capability="x", saga=saga, failure_status="failed")
    asyncio.run(runtime.run_stage(_ctx(spec, {"task_id": "t1"}, {})))
    # Only one attempt — the ValueError was not caught.
    assert calls["count"] == 1


def test_retry_attaches_to_current_step_only() -> None:
    """A `.retry()` call after step A and before step B applies only
    to A. B is not retried."""
    calls = {"a": 0, "b": 0}

    def _bump(name):
        def _impl(ctx):
            calls[name] += 1
            if calls[name] < 2:
                raise ConnectionError("flake")
            return name

        return _impl

    saga = (
        Saga("test")
        .deterministic("a", _bump("a"))
        .retry(attempts=5, on=(ConnectionError,))
        .deterministic("b", _bump("b"))
        .build()
    )
    runtime = QuadroSagaRuntime()
    spec = StageSpec(capability="x", saga=saga, failure_status="failed")
    result = asyncio.run(runtime.run_stage(_ctx(spec, {"task_id": "t1"}, {})))
    # A retried successfully; B failed on first try.
    assert calls["a"] == 2
    assert calls["b"] == 1
    assert result.terminal_reason.startswith("step_failed:b")


# ── deadline ───────────────────────────────────────────────────────────────────


def test_deadline_passes_when_step_completes_within_window() -> None:
    """A deadline does not interfere with a fast step — same output
    as without the modifier."""
    saga = (
        Saga("test")
        .deterministic("fast", lambda ctx: "done")
        .deadline(within=timedelta(seconds=5))
        .build()
    )
    runtime = QuadroSagaRuntime()
    spec = StageSpec(capability="x", saga=saga)
    result = asyncio.run(runtime.run_stage(_ctx(spec, {"task_id": "t1"}, {})))
    assert result.output == "done"


def test_deadline_fails_step_when_exceeded() -> None:
    """A step that takes longer than `within` is cancelled and the
    saga fails with terminal_reason `deadline_exceeded:<step>`."""

    async def _slow(ctx):
        await asyncio.sleep(0.5)
        return "should not arrive"

    saga = (
        Saga("test")
        .deterministic("slow", _slow)
        .deadline(within=timedelta(milliseconds=50))
        .build()
    )
    runtime = QuadroSagaRuntime()
    spec = StageSpec(capability="x", saga=saga, failure_status="timed_out")
    result = asyncio.run(runtime.run_stage(_ctx(spec, {"task_id": "t1"}, {})))
    assert result.terminal_reason == "deadline_exceeded:slow"
    assert result.status == "timed_out"


def test_deadline_combines_with_retry_correctly() -> None:
    """When a step has both retry and deadline, each retry attempt
    gets its own deadline window. Total wall-clock time is bounded
    by attempts * within (plus retry-loop overhead)."""
    attempts = {"count": 0}

    async def _slow(ctx):
        attempts["count"] += 1
        await asyncio.sleep(0.2)
        return "never"

    saga = (
        Saga("test")
        .deterministic("repeated_slow", _slow)
        .retry(attempts=3, on=(asyncio.TimeoutError,))
        .deadline(within=timedelta(milliseconds=50))
        .build()
    )
    runtime = QuadroSagaRuntime()
    spec = StageSpec(capability="x", saga=saga, failure_status="timed_out")
    asyncio.run(runtime.run_stage(_ctx(spec, {"task_id": "t1"}, {})))
    # Three attempts each with their own (failed) deadline.
    assert attempts["count"] == 3

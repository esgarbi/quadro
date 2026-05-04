from __future__ import annotations

import asyncio

from quadro import LocalA2ANetwork, QuadroBoard
from quadro.board.backends.sqlite import SqliteBoardBackend
from quadro.pipeline import StageSpec
from quadro.runtime_plugins.base import RuntimeContext
from quadro.runtime_plugins.saga import QuadroSagaRuntime
from quadro.saga import Saga as SagaAlias


def test_saga_resumes_across_simulated_worker_restart() -> None:
    """End-to-end integration: saga state lives on the Board, not in
    the runtime, so a SECOND ``run_stage`` invocation on the same task
    picks up where the first left off without re-executing completed
    steps.

    The "restart" is simulated by pre-populating saga state on the
    board to mimic a worker that crashed after one step had completed,
    then running the saga from a fresh runtime instance and verifying
    only the remaining steps execute.
    """
    board = QuadroBoard(SqliteBoardBackend(":memory:"), network=LocalA2ANetwork())
    bc = board.client()

    # A saga where each step increments a real counter, so we can
    # detect re-execution.
    counter = {"a": 0, "b": 0, "c": 0}

    def _bump(name):
        def _impl(ctx):
            counter[name] += 1
            return name
        return _impl

    saga = (
        SagaAlias("resume_test")
        .deterministic("a", _bump("a"))
        .deterministic("b", _bump("b"))
        .deterministic("c", _bump("c"))
        .build()
    )

    # Post a real task on the board so its task_id is well-formed.
    # post_task takes task_type as its first positional argument and
    # label as its second; passing task_type again as a kwarg here
    # would raise TypeError (duplicate binding).
    posted = bc.post_task("test", "resume integration")
    task_id = posted["task_id"]

    # Manually pre-populate saga state to simulate "the worker crashed
    # after step a completed". This is the same shape the runtime would
    # have written on its own; we shortcut the first invocation to keep
    # the test focused on the resume property.
    bc.put_data(
        f"_saga:{task_id}",
        {
            "saga_name": "resume_test",
            "pc": "b",
            "idempotency_key": None,
            "completed_steps": {"a": "a"},
            "evidence": {},
            "stamps": [],
            "fork_children": {},
            "waiting_for": None,
            "started_at": None,
            "sla_deadline": None,
        },
    )

    # The runtime sees the persisted state and resumes from step b.
    runtime = QuadroSagaRuntime()
    spec = StageSpec(capability="test", success_status="done", saga=saga)

    def _board_fn(intent: str, payload: dict) -> dict:
        return bc.request(intent, payload)

    task = bc.get_task(task_id)
    result = asyncio.run(runtime.run_stage(RuntimeContext(
        stage=spec,
        task=task,
        context={"payload": {"task": task}},
        board_fn=_board_fn,
    )))

    # Steps b and c ran; step a did NOT re-run.
    assert counter == {"a": 0, "b": 1, "c": 1}
    assert result.output == "c"
    assert result.terminal_reason == "saga_completed"

    # The final state on the board reflects all three steps complete.
    final_state = bc.get_data(f"_saga:{task_id}")
    assert final_state["pc"] is None
    assert set(final_state["completed_steps"].keys()) == {"a", "b", "c"}

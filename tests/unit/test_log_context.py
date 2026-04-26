"""Coverage for :mod:`quadro.log_context` and its plumbing in agents."""

from __future__ import annotations

import logging
from io import StringIO

from quadro import ChiefAgent, LocalA2ANetwork, QuadroBoard, WorkerAgent
from quadro.a2a.contracts import A2ARequest
from quadro.board.backends.sqlite import SqliteBoardBackend
from quadro.log_context import (
    QuadroContextFilter,
    agent_id_var,
    agent_scope,
    chief_cycle_id_var,
    chief_cycle_scope,
    task_id_var,
    task_scope,
)


def _make_env() -> tuple[LocalA2ANetwork, QuadroBoard, ChiefAgent]:
    network = LocalA2ANetwork()
    board = QuadroBoard(
        SqliteBoardBackend(":memory:"),
        profile_resolver={"work": "fast"},
        network=network,
    )
    chief = ChiefAgent.builder(board.client()).build()
    return network, board, chief


# ── Context variables: get/set/reset ────────────────────────────────────────


def test_task_scope_sets_and_resets() -> None:
    assert task_id_var.get() is None
    with task_scope("t-123"):
        assert task_id_var.get() == "t-123"
    assert task_id_var.get() is None


def test_nested_scopes_compose_cleanly() -> None:
    with task_scope("outer"):
        assert task_id_var.get() == "outer"
        with task_scope("inner"):
            assert task_id_var.get() == "inner"
        assert task_id_var.get() == "outer"


# ── Filter injects attributes on every record ───────────────────────────────


def _capture_log_output(filter_obj: logging.Filter) -> tuple[logging.Logger, StringIO]:
    buf = StringIO()
    handler = logging.StreamHandler(buf)
    handler.setLevel(logging.DEBUG)
    handler.addFilter(filter_obj)
    handler.setFormatter(
        logging.Formatter(
            "[task=%(quadro_task_id)s cycle=%(quadro_chief_cycle_id)s "
            "agent=%(quadro_agent_id)s] %(message)s"
        )
    )
    logger = logging.getLogger(f"quadro.test.{id(buf)}")
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    logger.propagate = False
    return logger, buf


def test_context_filter_injects_current_values() -> None:
    filter_obj = QuadroContextFilter()
    logger, buf = _capture_log_output(filter_obj)

    with task_scope("t-42"), chief_cycle_scope("c-99"), agent_scope("a-1"):
        logger.info("inside scope")
    logger.info("outside scope")

    output = buf.getvalue()
    assert "[task=t-42 cycle=c-99 agent=a-1] inside scope" in output
    assert "[task=- cycle=- agent=-] outside scope" in output


# ── End-to-end plumbing in chief and worker ──────────────────────────────────


def test_chief_decision_cycle_binds_cycle_id() -> None:
    """The chief must set ``chief_cycle_id_var`` while a cycle runs."""
    network, board, chief = _make_env()

    captured: list[str | None] = []

    def _capture_policy(chief_context: dict) -> None:
        captured.append(chief_cycle_id_var.get())

    chief_with_policy = (
        ChiefAgent.builder(board.client()).policy(_capture_policy).build()
    )
    chief_with_policy.wake(trigger="seed")

    assert len(captured) == 1
    assert captured[0] is not None
    # cycle IDs are short hex strings (uuid4().hex[:12])
    assert len(captured[0]) == 12
    # scope is released after wake returns
    assert chief_cycle_id_var.get() is None


def test_worker_execute_binds_task_and_agent_ids() -> None:
    """The worker must set ``task_id_var`` and ``agent_id_var`` while an execute_fn runs."""
    network, board, _ = _make_env()
    bc = board.client()

    captured: dict[str, str | None] = {}

    def _exec(context: dict, board_fn) -> str:
        captured["task_id"] = task_id_var.get()
        captured["agent_id"] = agent_id_var.get()
        return "ok"

    worker = (
        WorkerAgent.builder("worker_1", bc)
        .capability("work")
        .at("a2a://w1")
        .execute(_exec)
        .build()
    )
    worker.register()

    task = bc.post_task("work", "do it")
    # Manually drive the worker path — bypass Chief to keep the test small.
    resp = network.request(
        "a2a://w1",
        A2ARequest(
            intent="worker.execute_task",
            payload={"task_id": task["task_id"]},
        ).to_dict(),
    )
    assert resp["ok"], resp.get("error")

    assert captured.get("task_id") == task["task_id"]
    assert captured.get("agent_id") == "worker_1"
    # scopes released after execute returns
    assert task_id_var.get() is None
    assert agent_id_var.get() is None

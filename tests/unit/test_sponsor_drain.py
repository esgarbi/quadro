"""Drain integration tests — Chief cooperation and RunLoop lifecycle."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from quadro import (
    BoardClient,
    ChiefAgent,
    LifecycleBuilder,
    LocalA2ANetwork,
    QuadroBoard,
    RunLoop,
    dispatch_batch,
)
from quadro.board.backends.sqlite import SqliteBoardBackend
from quadro.dispatch import DRAIN_FLAG_KEY
from quadro.sponsor import (
    AllOf,
    AlwaysStopSponsor,
    Continue,
    Drain,
    GoalSponsor,
    Lease,
    ScriptedSponsor,
    Stop,
    TickBudgetSponsor,
)


def _make_env(profile: str = "fast") -> tuple[LocalA2ANetwork, BoardClient, ChiefAgent, QuadroBoard]:
    network = LocalA2ANetwork()
    board = QuadroBoard(
        SqliteBoardBackend(":memory:"),
        profile_resolver={"work": profile},
        network=network,
    )
    bc = board.client()
    chief = ChiefAgent.builder(bc).build()
    return network, bc, chief, board


# ── Chief drain flag ──────────────────────────────────────────────────────────


def test_chief_draining_blocks_new_assignments() -> None:
    network, bc, chief, board = _make_env()

    bc.register_agent(
        agent_id="worker1",
        name="Worker 1",
        url="a2a://worker1",
        version="1.0",
        capabilities=["work"],
        description="test",
    )
    bc.post_task("work", "Task A")

    # With no drain, routing picks up the UNASSIGNED task.
    chief.wake(trigger="seed")
    state = bc.full_state()
    task = [t for t in state["tasks"] if t["task_type"] == "work"][0]
    assert task["status"] == "IN_PROGRESS"
    assert task["assigned_to"] == "worker1"

    # Now with drain: a fresh UNASSIGNED task is NOT picked up.
    chief.set_draining(True)
    bc.post_task("work", "Task B")
    chief.wake(trigger="seed")
    state = bc.full_state()
    pending = [
        t for t in state["tasks"] if t["label"] == "Task B"
    ][0]
    assert pending["status"] == "UNASSIGNED"


# ── Dispatch batch respects drain flag on the board ───────────────────────────


def test_dispatch_batch_skips_unassigned_when_draining_via_board() -> None:
    network, bc, chief, board = _make_env()

    bc.register_agent(
        agent_id="writer1",
        name="Writer 1",
        url="a2a://writer1",
        version="1.0",
        capabilities=["work"],
        description="test",
    )
    bc.post_task("work", "to be skipped")
    bc.put_data(DRAIN_FLAG_KEY, True)

    def board_fn(intent: str, payload: dict) -> dict:
        return bc.request(intent, payload)

    dispatched, skipped = dispatch_batch(
        board_fn,
        network,
        worker_registry={"work": [("writer1", "a2a://writer1")]},
        status_filter="UNASSIGNED",
        target_status="IN_PROGRESS",
        capability="work",
    )

    assert dispatched == []
    # UNASSIGNED is filtered out by drain; so the task isn't even considered.
    assert skipped == []


# ── RunLoop drain lifecycle: empty board auto-stops ───────────────────────────


def test_run_loop_drain_with_empty_board_stops_immediately() -> None:
    _, bc, chief, board = _make_env()

    script = [
        Continue(lease=Lease(ticks=1)),
        Drain(deadline=None, reason="empty_queue"),
    ]
    state = (
        RunLoop(board, chief)
        .sponsor(ScriptedSponsor(script))
        .poll_every(0.0)
        .run()
    )
    assert "tasks" in state
    # Drain flag should be cleared after the run completes.
    assert bc.get_data(DRAIN_FLAG_KEY) is False


# ── RunLoop drain deadline enforcement ────────────────────────────────────────


def test_run_loop_drain_deadline_forces_stop() -> None:
    _, bc, chief, board = _make_env()

    # Plant a task that will never finish (no worker will pick it up while
    # draining, and we post it after the first tick below).
    bc.register_agent(
        agent_id="w1",
        name="Worker",
        url="a2a://w1",
        version="1.0",
        capabilities=["work"],
        description="test",
    )
    bc.post_task("work", "in_flight")

    # Simulate the task being in progress by assigning it directly.
    state = bc.full_state()
    tid = state["tasks"][0]["task_id"]
    bc.update_task(tid, "IN_PROGRESS", assigned_to="w1")

    now = datetime.now(timezone.utc)
    script = [
        # Drain with a 5ms deadline — drain will expire before the task
        # completes, forcing Stop.
        Drain(deadline=now + timedelta(milliseconds=5), reason="short_drain"),
    ]

    start = datetime.now(timezone.utc)
    state = (
        RunLoop(board, chief)
        .sponsor(ScriptedSponsor(script))
        .poll_every(0.0)
        .run()
    )
    elapsed = datetime.now(timezone.utc) - start
    # Run exited due to drain deadline expiring, not because tasks drained
    # (the task is still IN_PROGRESS).
    assert elapsed < timedelta(seconds=5)
    still_in_flight = [t for t in state["tasks"] if t["status"] == "IN_PROGRESS"]
    assert still_in_flight, "task should still be in flight at forced stop"


# ── Drain default 5-minute fallback ───────────────────────────────────────────


def test_run_loop_uses_configured_drain_max_duration() -> None:
    _, bc, chief, board = _make_env()

    # Drain with deadline=None forces the runtime's fallback. Use a tiny one
    # so the test is fast. Pair with an in-flight task that never completes.
    bc.register_agent(
        agent_id="w1",
        name="Worker",
        url="a2a://w1",
        version="1.0",
        capabilities=["work"],
        description="test",
    )
    bc.post_task("work", "stuck")
    state = bc.full_state()
    tid = state["tasks"][0]["task_id"]
    bc.update_task(tid, "IN_PROGRESS", assigned_to="w1")

    script = [Drain(deadline=None, reason="default_fallback")]
    start = datetime.now(timezone.utc)
    (
        RunLoop(board, chief)
        .sponsor(ScriptedSponsor(script))
        .poll_every(0.0)
        .drain_max_duration(timedelta(milliseconds=10))
        .run()
    )
    elapsed = datetime.now(timezone.utc) - start
    assert elapsed < timedelta(seconds=5)


# ── Telemetry reflects draining state ─────────────────────────────────────────


def test_chief_set_draining_updates_board_telemetry() -> None:
    network, bc, chief, board = _make_env()

    chief.set_draining(True)
    telem = bc.full_state()["data"].get("_chief_telemetry", {})
    assert telem.get("draining") is True

    chief.set_draining(False)
    telem = bc.full_state()["data"].get("_chief_telemetry", {})
    assert telem.get("draining") is False

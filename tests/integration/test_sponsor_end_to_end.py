"""End-to-end Sponsor/Lease integration — real board, real chief, real lifecycle.

Exercises the canonical "goal + safety cap + drain" story against a
``SqliteBoardBackend`` so every surface is touched: runtime, chief, board
events, sponsor decisions, drain coordination, shutdown.
"""

from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from quadro import (
    BoardClient,
    ChiefAgent,
    LifecycleBuilder,
    LocalA2ANetwork,
    QuadroBoard,
    QuadroRuntime,
    WorkerPool,
)
from quadro.board.backends.sqlite import SqliteBoardBackend
from quadro.sponsor import (
    AllOf,
    Continue,
    DeadlineSponsor,
    Drain,
    GoalSponsor,
    Lease,
    Priority,
    QueueDepthSponsor,
    ScriptedSponsor,
    Sponsor,
    SponsorContext,
    Stop,
    TickBudgetSponsor,
)


def _runtime_with_backend(tmp_path: Path) -> QuadroRuntime:
    """Use the built-in 'fast' profile so the default chief routing drives tasks
    through UNASSIGNED -> IN_PROGRESS -> COMPLETE without custom policy."""
    db_path = tmp_path / "sponsor.db"
    runtime = QuadroRuntime(SqliteBoardBackend(str(db_path))).with_profiles(
        profile_resolver={"work": "fast"},
    )
    return runtime


def _build_manual_pipeline(runtime: QuadroRuntime):
    """Assemble a pool + chief + ombudsman by hand (no MAF dependency)."""
    bc = runtime.client

    def _done_executor(context: dict, board_fn) -> str:
        task = context["payload"]["task"]
        board_fn(
            "worker.post_result",
            {
                "task_id": task["task_id"],
                "output": "ok",
                "agent_id": context.get("agent_id"),
            },
        )
        return "ok"

    pool = (
        WorkerPool(bc)
        .workers(2)
        .wakes("a2a://chief")
        .add("work", _done_executor)
        .build()
    )
    chief = ChiefAgent.builder(bc).at("a2a://chief").build()
    ombudsman = pool.ombudsman()
    from types import SimpleNamespace

    return SimpleNamespace(chief=chief, ombudsman=ombudsman)


def test_end_to_end_goal_plus_safety_cap(tmp_path: Path) -> None:
    runtime = _runtime_with_backend(tmp_path)
    pipeline = _build_manual_pipeline(runtime)

    # Plant three tasks.
    for i in range(3):
        runtime.client.post_task("work", f"Task {i}")

    final = (
        runtime.sponsor(
            AllOf(
                GoalSponsor(
                    lambda s: sum(
                        1 for t in s["tasks"] if t["status"] == "COMPLETE"
                    )
                    >= 3
                ),
                TickBudgetSponsor(200),
                DeadlineSponsor.from_now(seconds=10),
            )
        )
        .poll_every(0.01)
        .ombudsman_every(0.05)
        .run(pipeline)
    )

    done_count = sum(1 for t in final["tasks"] if t["status"] == "COMPLETE")
    assert done_count == 3

    log = final["data"].get("_sponsor_log") or []
    assert log[-1]["decision"] == "stop"
    assert "goal_met" in log[-1]["reason"]


def test_end_to_end_drain_completes_when_in_flight_finishes(
    tmp_path: Path,
) -> None:
    runtime = _runtime_with_backend(tmp_path)
    pipeline = _build_manual_pipeline(runtime)

    # Plant two tasks.
    for i in range(2):
        runtime.client.post_task("work", f"Task {i}")

    script = [
        Continue(lease=Lease(ticks=1)),
        Continue(lease=Lease(ticks=1)),
        Continue(lease=Lease(ticks=1)),
        Continue(lease=Lease(ticks=1)),
        Drain(deadline=None, reason="wind_down"),
    ]
    runtime.sponsor(ScriptedSponsor(script))
    final = runtime.poll_every(0.01).ombudsman_every(0.05).run(pipeline)

    assert all(t["status"] == "COMPLETE" for t in final["tasks"])
    log = final["data"].get("_sponsor_log") or []
    decisions = [e["decision"] for e in log]
    assert "drain" in decisions


def test_end_to_end_sponsor_log_records_lease_chain(tmp_path: Path) -> None:
    runtime = _runtime_with_backend(tmp_path)
    pipeline = _build_manual_pipeline(runtime)

    # One task, run it through.
    runtime.client.post_task("work", "Solo")

    runtime.sponsor(
        AllOf(
            GoalSponsor(
                lambda s: all(t["status"] == "COMPLETE" for t in s["tasks"])
                and bool(s["tasks"])
            ),
            TickBudgetSponsor(50),
        )
    ).poll_every(0.01).ombudsman_every(0.05).run(pipeline)

    log = runtime.client.get_data("_sponsor_log") or []
    continue_entries = [e for e in log if e["decision"] == "continue"]
    # Renewals should chain: the second Continue's renewal_of == first's lease.id.
    if len(continue_entries) >= 2:
        first_lease_id = continue_entries[0]["lease"]["id"]
        assert continue_entries[1]["lease"]["renewal_of"] == first_lease_id

    status = runtime.client.get_data("_sponsor_status")
    assert isinstance(status, dict)
    assert status["active_lease"] is None  # cleared on finalise
    assert status["draining"] is False
    assert status["sponsor_id"] == "all_of"

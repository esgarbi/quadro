from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from quadro import BoardClient, ChiefAgent, LocalA2ANetwork, QuadroBoard, RunLoop
from quadro.board.backends.sqlite import SqliteBoardBackend
from quadro.sponsor import (
    AllOf,
    AlwaysOnSponsor,
    AlwaysStopSponsor,
    Continue,
    Drain,
    GoalSponsor,
    Lease,
    ScriptedSponsor,
    Stop,
    TickBudgetSponsor,
)


def _make_env() -> tuple[LocalA2ANetwork, str, BoardClient, ChiefAgent, QuadroBoard]:
    network = LocalA2ANetwork()
    board = QuadroBoard(
        SqliteBoardBackend(":memory:"),
        profile_resolver={"work": "fast"},
        network=network,
    )
    bc = board.client()
    chief = ChiefAgent.builder(bc).build()
    return network, board._url, bc, chief, board


# ── 1. run() raises without a Sponsor ─────────────────────────────────────────


def test_run_loop_raises_without_sponsor() -> None:
    _, _, bc, chief, _ = _make_env()
    with pytest.raises(ValueError, match="sponsor"):
        RunLoop(bc, chief).run()


# ── 2. run() calls chief.nudge() on seed ──────────────────────────────────────


def test_run_loop_calls_chief_nudge_on_seed() -> None:
    _, _, bc, chief, board = _make_env()
    chief.nudge = MagicMock(return_value=1)  # type: ignore[method-assign]

    RunLoop(board, chief).sponsor(AlwaysStopSponsor()).poll_every(0.0).run()

    chief.nudge.assert_called()
    assert chief.nudge.call_count >= 1


# ── 3. run() exits when GoalSponsor predicate becomes true ────────────────────


def test_run_loop_exits_when_goal_met() -> None:
    _, _, bc, chief, board = _make_env()

    call_count = 0

    def done_after_first(state: dict) -> bool:
        nonlocal call_count
        call_count += 1
        return True

    state = (
        RunLoop(board, chief)
        .sponsor(GoalSponsor(done_after_first))
        .poll_every(0.0)
        .run()
    )
    assert call_count == 1
    assert isinstance(state, dict)
    assert "tasks" in state


# ── 4. on_cycle and on_complete callbacks are called ──────────────────────────


def test_run_loop_calls_on_cycle_and_on_complete() -> None:
    _, _, bc, chief, board = _make_env()

    cycle_calls: list[tuple[dict, int]] = []
    complete_calls: list[dict] = []

    cycle_target = 3
    cycle_counter = 0

    def done_after_n(state: dict) -> bool:
        nonlocal cycle_counter
        cycle_counter += 1
        return cycle_counter >= cycle_target

    (
        RunLoop(board, chief)
        .sponsor(AllOf(GoalSponsor(done_after_n), TickBudgetSponsor(10)))
        .on_cycle(lambda s, c: cycle_calls.append((s, c)))
        .on_complete(lambda s: complete_calls.append(s))
        .poll_every(0.0)
        .run()
    )

    assert len(cycle_calls) >= 1
    assert len(complete_calls) == 1
    assert "tasks" in complete_calls[0]


# ── 5. ombudsman fires chief.nudge() ──────────────────────────────────────────


def test_run_loop_ombudsman_fires_chief_nudge() -> None:
    _, _, bc, chief, board = _make_env()
    chief.nudge = MagicMock(return_value=1)  # type: ignore[method-assign]

    # Run enough ticks for the ombudsman window (0.1s) to elapse at least once
    # on top of the seed nudge. poll_every=0.05s, 10 ticks -> ~0.5s elapsed.
    (
        RunLoop(board, chief)
        .sponsor(TickBudgetSponsor(10))
        .poll_every(0.05)
        .ombudsman_every(0.1)
        .run()
    )

    # seed nudge + at least one ombudsman nudge
    assert chief.nudge.call_count >= 2


# ── 6. TickBudgetSponsor enforces safety cap ──────────────────────────────────


def test_run_loop_tick_budget_stops_loop() -> None:
    _, _, bc, chief, board = _make_env()

    (
        RunLoop(board, chief)
        .sponsor(TickBudgetSponsor(3))
        .poll_every(0.0)
        .run()
    )


# ── 7. Drain lifecycle: no active tasks -> auto Stop ──────────────────────────


def test_run_loop_drain_stops_when_no_active_tasks() -> None:
    _, _, bc, chief, board = _make_env()

    script = [
        Continue(lease=Lease(ticks=1)),
        Drain(deadline=None, reason="wind_down"),
    ]
    sponsor = ScriptedSponsor(script)

    state = (
        RunLoop(board, chief)
        .sponsor(sponsor)
        .poll_every(0.0)
        .run()
    )
    assert "tasks" in state


# ── 8. Sponsor exceptions fail closed -> Stop ─────────────────────────────────


def test_run_loop_sponsor_exception_is_treated_as_stop() -> None:
    _, _, bc, chief, board = _make_env()

    class _Exploder:
        name = "exploder"
        fail_open = False

        def propose_lease(self, ctx, prior):  # noqa: D401
            raise RuntimeError("kaboom")

    state = (
        RunLoop(board, chief)
        .sponsor(_Exploder())
        .poll_every(0.0)
        .run()
    )
    assert "tasks" in state


# ── 9. Sponsor log is persisted to the board ──────────────────────────────────


def test_run_loop_persists_sponsor_log() -> None:
    _, _, bc, chief, board = _make_env()

    (
        RunLoop(board, chief)
        .sponsor(AllOf(GoalSponsor(lambda s: True), TickBudgetSponsor(5)))
        .poll_every(0.0)
        .run()
    )

    data = bc.full_state()["data"]
    log = data.get("_sponsor_log")
    assert isinstance(log, list) and len(log) >= 1
    assert log[0]["decision"] in {"continue", "drain", "stop"}


# ── 10. Drain flag is published during drain ──────────────────────────────────


def test_run_loop_publishes_drain_flag() -> None:
    _, _, bc, chief, board = _make_env()

    script = [
        Drain(deadline=None, reason="drain_now"),
    ]
    sponsor = ScriptedSponsor(script)

    (
        RunLoop(board, chief)
        .sponsor(sponsor)
        .poll_every(0.0)
        .run()
    )

    from quadro.dispatch import DRAIN_FLAG_KEY

    # After the run completes the flag should be cleared.
    assert bc.get_data(DRAIN_FLAG_KEY) is False


# ── 11. Sponsor status snapshot is published for the UI ───────────────────────


def test_run_loop_publishes_sponsor_status() -> None:
    _, _, bc, chief, board = _make_env()

    (
        RunLoop(board, chief)
        .sponsor(AllOf(GoalSponsor(lambda s: False), TickBudgetSponsor(2)))
        .poll_every(0.0)
        .run()
    )

    status = bc.get_data("_sponsor_status")
    assert isinstance(status, dict)
    # Final status after the loop exits has active_lease=None.
    assert status["active_lease"] is None
    assert status["draining"] is False
    assert status["sponsor_id"] == "all_of"
    assert "meters" in status and isinstance(status["meters"], dict)
    assert "updated_at" in status

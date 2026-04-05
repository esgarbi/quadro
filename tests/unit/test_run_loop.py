from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from quadro import BoardClient, ChiefAgent, LocalA2ANetwork, QuadroBoard, RunLoop
from quadro.board.backends.sqlite import SqliteBoardBackend


def _make_env() -> tuple[LocalA2ANetwork, str, BoardClient, ChiefAgent]:
    network = LocalA2ANetwork()
    board_url = "a2a://board"
    board = QuadroBoard(
        SqliteBoardBackend(":memory:"),
        profile_resolver={"work": "fast"},
    )
    network.register_endpoint(board_url, board.handle_request)
    bc = BoardClient(network, board_url)
    chief = ChiefAgent.builder(bc).build()
    return network, board_url, bc, chief


# ── 1. run() raises without done_when ─────────────────────────────────────────


def test_run_loop_raises_without_done_when() -> None:
    _, _, bc, chief = _make_env()
    with pytest.raises(ValueError, match="done_when"):
        RunLoop(bc, chief).run()


# ── 2. run() calls chief.nudge() on seed ──────────────────────────────────────


def test_run_loop_calls_chief_nudge_on_seed() -> None:
    _, _, bc, chief = _make_env()
    chief.nudge = MagicMock(return_value=1)  # type: ignore[method-assign]

    RunLoop(bc, chief).done_when(lambda s: True).poll_every(0.0).run()

    assert chief.nudge.call_count >= 1
    chief.nudge.assert_called()


# ── 3. run() exits when done_when returns True ────────────────────────────────


def test_run_loop_exits_when_done() -> None:
    _, _, bc, chief = _make_env()

    call_count = 0

    def done_after_first(state: dict) -> bool:
        nonlocal call_count
        call_count += 1
        return True

    state = RunLoop(bc, chief).done_when(done_after_first).poll_every(0.0).run()

    assert call_count == 1
    assert isinstance(state, dict)
    assert "tasks" in state


# ── 4. on_cycle and on_complete callbacks are called ──────────────────────────


def test_run_loop_calls_on_cycle_and_on_complete() -> None:
    _, _, bc, chief = _make_env()

    cycle_calls: list[tuple[dict, int]] = []
    complete_calls: list[dict] = []

    cycle_target = 3
    cycle_counter = 0

    def done_after_n(state: dict) -> bool:
        nonlocal cycle_counter
        cycle_counter += 1
        return cycle_counter >= cycle_target

    def on_cycle(state: dict, cycle: int) -> None:
        cycle_calls.append((state, cycle))

    def on_complete(state: dict) -> None:
        complete_calls.append(state)

    RunLoop(bc, chief).done_when(done_after_n).on_cycle(on_cycle).on_complete(
        on_complete
    ).poll_every(0.0).run()

    assert len(cycle_calls) == cycle_target
    assert cycle_calls[0][1] == 0
    assert cycle_calls[1][1] == 1
    assert cycle_calls[2][1] == 2
    assert len(complete_calls) == 1
    assert "tasks" in complete_calls[0]


# ── 5. ombudsman fires chief.nudge() ──────────────────────────────────────────


def test_run_loop_ombudsman_fires_chief_nudge() -> None:
    _, _, bc, chief = _make_env()
    chief.nudge = MagicMock(return_value=1)  # type: ignore[method-assign]

    call_limit = 3
    counter = 0

    def done_after_n(state: dict) -> bool:
        nonlocal counter
        counter += 1
        return counter >= call_limit

    RunLoop(bc, chief).done_when(done_after_n).poll_every(0.05).ombudsman_every(
        0.1
    ).max_cycles(50).run()

    # seed nudge + at least one ombudsman nudge
    assert chief.nudge.call_count >= 2

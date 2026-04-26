from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from quadro import LifecycleBuilder, LocalA2ANetwork, QuadroBoard, QuadroRuntime
from quadro.board.backends.sqlite import SqliteBoardBackend
from quadro.sponsor import (
    AllOf,
    GoalSponsor,
    TickBudgetSponsor,
)


def test_runtime_starts_from_backend_and_creates_board_lazily() -> None:
    network = LocalA2ANetwork()
    runtime = QuadroRuntime(SqliteBoardBackend(":memory:"), network=network)

    assert runtime._board is None
    assert runtime.network is network

    board = runtime.board

    assert isinstance(board, QuadroBoard)
    assert runtime._board is board
    assert runtime.client.network is network


def test_runtime_put_data_and_profiles_are_fluent_before_board_creation() -> None:
    lifecycle = (
        LifecycleBuilder()
        .step("UNASSIGNED", "drafting")
        .step("drafting", "published")
        .build()
    )
    runtime = QuadroRuntime(SqliteBoardBackend(":memory:")).with_profiles(
        profile_resolver={"article": "article"},
        custom_profiles={"article": lifecycle},
    )

    assert runtime.put_data("goal", {"target_articles": 2}) is runtime
    state = runtime.client.full_state()

    assert state["data"]["goal"] == {"target_articles": 2}
    assert state["data"]["_col_order"] == ["UNASSIGNED", "drafting", "published"]


def test_runtime_configuration_cannot_change_after_board_creation() -> None:
    runtime = QuadroRuntime(SqliteBoardBackend(":memory:"))

    _ = runtime.client

    with pytest.raises(RuntimeError, match="configuration cannot change"):
        runtime.with_profiles(profile_resolver={"work": "fast"})

    with pytest.raises(RuntimeError, match="configuration cannot change"):
        runtime.with_network(LocalA2ANetwork())


def test_runtime_raises_without_sponsor() -> None:
    runtime = QuadroRuntime(SqliteBoardBackend(":memory:"))
    built_pipeline = SimpleNamespace(chief=MagicMock())

    with pytest.raises(ValueError, match="sponsor"):
        runtime.run(built_pipeline)


def test_runtime_run_delegates_callbacks_and_ombudsman() -> None:
    chief = MagicMock()
    ombudsman = MagicMock()
    cycle_calls: list[tuple[dict, int]] = []
    complete_calls: list[dict] = []
    done_calls = 0

    def done_after_second_cycle(state: dict) -> bool:
        nonlocal done_calls
        done_calls += 1
        return done_calls >= 3

    runtime = (
        QuadroRuntime(SqliteBoardBackend(":memory:"))
        .sponsor(AllOf(GoalSponsor(done_after_second_cycle), TickBudgetSponsor(10)))
        .on_cycle(lambda state, cycle: cycle_calls.append((state, cycle)))
        .on_complete(complete_calls.append)
        .poll_every(0.0)
        .ombudsman_every(0.0)
    )

    state = runtime.run(SimpleNamespace(chief=chief, ombudsman=ombudsman))

    assert state == complete_calls[0]
    assert cycle_calls  # at least one cycle ran
    chief.nudge.assert_any_call(trigger="seed")
    chief.nudge.assert_any_call(trigger="ombudsman")
    ombudsman.nudge.assert_called()


def test_runtime_shutdown_hooks_run_after_successful_run() -> None:
    hook = MagicMock()
    resource = SimpleNamespace(stop=MagicMock())
    chief = MagicMock()
    runtime = (
        QuadroRuntime(SqliteBoardBackend(":memory:"))
        .add_shutdown_hook(hook)
        .sponsor(GoalSponsor(lambda state: True))
        .poll_every(0.0)
    )

    assert runtime.manage(resource) is resource

    runtime.run(SimpleNamespace(chief=chief))

    resource.stop.assert_called_once_with()
    hook.assert_called_once_with()


def test_runtime_drain_max_duration_setter_is_fluent() -> None:
    from datetime import timedelta

    runtime = QuadroRuntime(SqliteBoardBackend(":memory:")).drain_max_duration(
        timedelta(seconds=30)
    )
    assert runtime._drain_max_duration == timedelta(seconds=30)

from __future__ import annotations

import pytest

from quadro import BoardClient, ChiefAgent, LocalA2ANetwork, QuadroBoard, WorkerAgent
from quadro.board.backends.sqlite import SqliteBoardBackend


def _make_client() -> BoardClient:
    network = LocalA2ANetwork()
    board_url = "a2a://board"
    board = QuadroBoard(SqliteBoardBackend(":memory:"))
    network.register_endpoint(board_url, board.handle_request)
    return BoardClient(network, board_url)


def test_worker_builder_produces_correct_agent() -> None:
    bc = _make_client()
    fn = lambda ctx, _: "done"  # noqa: E731

    worker = (
        WorkerAgent.builder("my_worker", bc)
        .name("My Worker")
        .capability("writing", "review")
        .at("a2a://workers/my")
        .execute(fn)
        .wakes("a2a://chief")
        .build()
    )

    assert worker.agent_id == "my_worker"
    assert worker.name == "My Worker"
    assert worker.capabilities == ["writing", "review"]
    assert worker.url == "a2a://workers/my"
    assert worker.execute_fn is fn
    assert worker._chief_url == "a2a://chief"
    assert worker.board_url == bc._board_url
    assert worker.network is bc._network


def test_worker_builder_raises_without_url() -> None:
    bc = _make_client()
    with pytest.raises(ValueError, match="requires .at"):
        WorkerAgent.builder("w", bc).execute(lambda ctx, _: "x").build()


def test_worker_builder_raises_without_execute() -> None:
    bc = _make_client()
    with pytest.raises(ValueError, match="requires .execute"):
        WorkerAgent.builder("w", bc).at("a2a://w").build()


def test_chief_builder_produces_correct_agent() -> None:
    bc = _make_client()
    policy_fn = lambda ctx: None  # noqa: E731

    chief = ChiefAgent.builder(bc).at("a2a://chief").policy(policy_fn).build()

    assert chief.board_url == bc._board_url
    assert chief.network is bc._network
    assert chief._chief_url == "a2a://chief"
    assert chief._policy is policy_fn


def test_existing_constructors_unchanged() -> None:
    """Direct construction (no builder) still works identically."""
    bc = _make_client()

    worker = WorkerAgent(
        agent_id="w1",
        name="W1",
        capabilities=["test"],
        url="a2a://w1",
        board_url=bc._board_url,
        network=bc._network,
        execute_fn=lambda ctx, _: "ok",
    )
    assert worker.agent_id == "w1"

    chief = ChiefAgent(network=bc._network, board_url=bc._board_url)
    assert chief.board_url == bc._board_url

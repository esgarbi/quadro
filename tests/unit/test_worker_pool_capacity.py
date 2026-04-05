from __future__ import annotations

from quadro import BoardClient, LocalA2ANetwork, QuadroBoard, WorkerPool
from quadro.board.backends.sqlite import SqliteBoardBackend


def _make_env() -> tuple[LocalA2ANetwork, str, BoardClient]:
    network = LocalA2ANetwork()
    board_url = "a2a://board"
    board = QuadroBoard(
        SqliteBoardBackend(":memory:"),
        profile_resolver={"cap1": "fast", "cap2": "fast"},
    )
    network.register_endpoint(board_url, board.handle_request)
    bc = BoardClient(network, board_url)
    return network, board_url, bc


def _noop(ctx: dict, board_fn) -> str:
    return "ok"


def test_pool_workers_sets_agent_count() -> None:
    _, _, bc = _make_env()
    pool = WorkerPool(bc).workers(3).add("cap1", _noop).add("cap2", _noop).build()
    assert len(pool.agents) == 6


def test_pool_capacity_explicit() -> None:
    _, _, bc = _make_env()
    pool = (
        WorkerPool(bc)
        .workers(2)
        .capacity(10)
        .add("cap1", _noop)
        .add("cap2", _noop)
        .build()
    )
    assert pool.capacity() == 10


def test_pool_capacity_default_is_workers_times_capabilities() -> None:
    _, _, bc = _make_env()
    pool = WorkerPool(bc).workers(3).add("cap1", _noop).add("cap2", _noop).build()
    assert pool.capacity() == 6

from __future__ import annotations

import sqlite3
from threading import RLock

from quadro import BoardClient, ConflictError, LocalA2ANetwork, QuadroBoard
from quadro.a2a.contracts import A2ARequest
from quadro.board.backends.sqlite import SqliteBoardBackend
from quadro.board.idempotency import IdempotencyStore


def _make_env(
    *, with_store: bool = True
) -> tuple[LocalA2ANetwork, str, BoardClient, IdempotencyStore | None]:
    network = LocalA2ANetwork()
    board_url = "a2a://board"
    backend = SqliteBoardBackend(":memory:")

    store: IdempotencyStore | None = None
    if with_store:
        store = IdempotencyStore(backend._conn, backend._lock)

    board = QuadroBoard(
        backend,
        profile_resolver={"work": "fast"},
        network=network,
        url=board_url,
        idempotency_store=store,
    )
    bc = board.client()
    return network, board_url, bc, store


def test_duplicate_key_same_payload_returns_cached() -> None:
    network, url, bc, _ = _make_env()

    resp1 = network.request(
        url,
        A2ARequest(
            intent="board.post_task",
            payload={"task_type": "work", "label": "first"},
            idempotency_key="key-1",
        ).to_dict(),
    )
    assert resp1["ok"]
    task_id_1 = resp1["result"]["task"]["task_id"]

    resp2 = network.request(
        url,
        A2ARequest(
            intent="board.post_task",
            payload={"task_type": "work", "label": "first"},
            idempotency_key="key-1",
        ).to_dict(),
    )
    assert resp2["ok"]
    task_id_2 = resp2["result"]["task"]["task_id"]

    assert task_id_1 == task_id_2, "Duplicate key should return cached result"

    state = bc.full_state()
    task_count = len(state["tasks"])
    assert task_count == 1, f"Expected 1 task, got {task_count}"


def test_duplicate_key_different_payload_returns_conflict() -> None:
    network, url, _, _ = _make_env()

    resp1 = network.request(
        url,
        A2ARequest(
            intent="board.post_task",
            payload={"task_type": "work", "label": "first"},
            idempotency_key="key-2",
        ).to_dict(),
    )
    assert resp1["ok"]

    resp2 = network.request(
        url,
        A2ARequest(
            intent="board.post_task",
            payload={"task_type": "work", "label": "different label"},
            idempotency_key="key-2",
        ).to_dict(),
    )
    assert not resp2["ok"]
    assert "already used" in resp2["error"]


def test_no_key_executes_normally() -> None:
    _, _, bc, _ = _make_env()

    t1 = bc.post_task("work", "task A")
    t2 = bc.post_task("work", "task B")

    assert t1["task_id"] != t2["task_id"]

    state = bc.full_state()
    assert len(state["tasks"]) == 2


def test_board_without_store_behaves_as_before() -> None:
    network, url, bc, _ = _make_env(with_store=False)

    resp1 = network.request(
        url,
        A2ARequest(
            intent="board.post_task",
            payload={"task_type": "work", "label": "first"},
            idempotency_key="key-3",
        ).to_dict(),
    )
    assert resp1["ok"]

    resp2 = network.request(
        url,
        A2ARequest(
            intent="board.post_task",
            payload={"task_type": "work", "label": "first"},
            idempotency_key="key-3",
        ).to_dict(),
    )
    assert resp2["ok"]

    state = bc.full_state()
    assert len(state["tasks"]) == 2, "Without store, duplicate keys create new tasks"

from __future__ import annotations

import time

from quadro import BoardClient, LocalA2ANetwork, QuadroBoard
from quadro.board.backends.sqlite import SqliteBoardBackend


def _make_client() -> BoardClient:
    network = LocalA2ANetwork()
    board_url = "a2a://board"
    board = QuadroBoard(SqliteBoardBackend(":memory:"))
    network.register_endpoint(board_url, board.handle_request)
    return BoardClient(network, board_url)


def test_task_default_priority_is_5() -> None:
    client = _make_client()
    task = client.post_task("research", "gut health")
    assert task["priority"] == 5


def test_task_custom_priority_stored() -> None:
    client = _make_client()
    task = client.post_task("research", "urgent topic", priority=1)
    assert task["priority"] == 1


def test_list_tasks_ordered_by_priority() -> None:
    client = _make_client()
    client.post_task("research", "low priority", priority=9)
    time.sleep(0.01)
    client.post_task("research", "high priority", priority=1)
    time.sleep(0.01)
    client.post_task("research", "medium priority", priority=5)

    state = client.full_state()
    priorities = [t["priority"] for t in state["tasks"]]
    assert priorities == [1, 5, 9]


def test_priority_then_created_at_ordering() -> None:
    client = _make_client()
    t1 = client.post_task("research", "first at p3", priority=3)
    time.sleep(0.01)
    t2 = client.post_task("research", "second at p3", priority=3)

    state = client.full_state()
    ids = [t["task_id"] for t in state["tasks"]]
    assert ids == [t1["task_id"], t2["task_id"]]

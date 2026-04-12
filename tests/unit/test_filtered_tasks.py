from __future__ import annotations

from quadro import BoardClient, LocalA2ANetwork, QuadroBoard
from quadro.board.backends.sqlite import SqliteBoardBackend


def _make_env() -> tuple[LocalA2ANetwork, BoardClient]:
    network = LocalA2ANetwork()
    board = QuadroBoard(
        SqliteBoardBackend(":memory:"),
        profile_resolver={"work": "fast"},
        network=network,
        url="a2a://board",
    )
    return network, board.client()


def test_list_tasks_by_status_returns_matching_only() -> None:
    _, bc = _make_env()
    bc.post_task("work", "task A")
    bc.post_task("work", "task B")

    tasks = bc.list_tasks_by_status({"UNASSIGNED"})
    assert len(tasks) == 2
    assert all(t["status"] == "UNASSIGNED" for t in tasks)


def test_list_tasks_by_status_filters_out_non_matching() -> None:
    _, bc = _make_env()
    bc.post_task("work", "task A")
    bc.post_task("work", "task B")

    tasks = bc.list_tasks_by_status({"IN_PROGRESS"})
    assert len(tasks) == 0


def test_list_tasks_by_status_multiple_statuses() -> None:
    _, bc = _make_env()
    t1 = bc.post_task("work", "task A")
    bc.post_task("work", "task B")
    bc.update_task(t1["task_id"], "IN_PROGRESS", assigned_to="w1")

    tasks = bc.list_tasks_by_status({"UNASSIGNED", "IN_PROGRESS"})
    assert len(tasks) == 2
    statuses = {t["status"] for t in tasks}
    assert statuses == {"UNASSIGNED", "IN_PROGRESS"}


def test_list_tasks_by_status_empty_set_returns_empty() -> None:
    _, bc = _make_env()
    bc.post_task("work", "task A")

    tasks = bc.list_tasks_by_status(set())
    assert tasks == []


def test_list_tasks_by_status_preserves_priority_ordering() -> None:
    _, bc = _make_env()
    bc.post_task("work", "low priority", priority=9)
    bc.post_task("work", "high priority", priority=1)
    bc.post_task("work", "medium priority", priority=5)

    tasks = bc.list_tasks_by_status({"UNASSIGNED"})
    priorities = [t["priority"] for t in tasks]
    assert priorities == [1, 5, 9]

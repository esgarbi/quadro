from __future__ import annotations

from quadro import BoardClient, LocalA2ANetwork, QuadroBoard, WorkerAgent
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


def _complete_task(bc: BoardClient, network: LocalA2ANetwork) -> dict:
    """Post a task and move it to COMPLETE via the fast profile."""
    worker = (
        WorkerAgent.builder("w1", bc)
        .capability("work")
        .at("a2a://w1")
        .execute(lambda ctx, _: "done")
        .build()
    )
    worker.register()
    task = bc.post_task("work", "archival test")
    bc.update_task(task["task_id"], "IN_PROGRESS", assigned_to="w1")
    bc.update_task(task["task_id"], "COMPLETE")
    return task


def test_archive_terminal_task_removes_from_list_tasks() -> None:
    network, bc = _make_env()
    task = _complete_task(bc, network)

    result = bc.archive_task(task["task_id"])
    assert result is True

    state = bc.full_state()
    active_ids = {t["task_id"] for t in state["tasks"]}
    assert task["task_id"] not in active_ids


def test_archive_non_terminal_task_rejected() -> None:
    _, bc = _make_env()
    task = bc.post_task("work", "still active")

    try:
        bc.archive_task(task["task_id"])
        assert False, "Should have raised"
    except RuntimeError as exc:
        assert "terminal" in str(exc).lower()


def test_archived_task_still_retrievable_via_get_task() -> None:
    network, bc = _make_env()
    task = _complete_task(bc, network)
    task_id = task["task_id"]

    bc.archive_task(task_id)

    retrieved = bc.get_task(task_id)
    assert retrieved["task_id"] == task_id
    assert retrieved["status"] == "COMPLETE"


def test_archived_task_history_preserved() -> None:
    network, bc = _make_env()
    task = _complete_task(bc, network)
    task_id = task["task_id"]

    events_before = bc.task_history(task_id)
    assert len(events_before) > 0

    bc.archive_task(task_id)

    events_after = bc.task_history(task_id)
    assert events_after == events_before


def test_full_state_excludes_archived_tasks() -> None:
    network, bc = _make_env()
    t1 = _complete_task(bc, network)
    t2 = bc.post_task("work", "still active")

    bc.archive_task(t1["task_id"])

    state = bc.full_state()
    task_ids = {t["task_id"] for t in state["tasks"]}
    assert t1["task_id"] not in task_ids
    assert t2["task_id"] in task_ids

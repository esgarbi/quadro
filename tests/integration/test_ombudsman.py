from __future__ import annotations

from quadro import ChiefAgent, LocalA2ANetwork, QuadroBoard, WorkerAgent
from quadro.a2a.contracts import A2ARequest
from quadro.board.backends.sqlite import SqliteBoardBackend
from quadro.board.records import TaskStatus
from quadro.ombudsman import Ombudsman


def _make_env(profile: str = "fast") -> tuple[LocalA2ANetwork, str, str]:
    """Returns (network, board_url, task_id) with one IN_PROGRESS task."""
    network = LocalA2ANetwork()
    board_url = "a2a://board"
    board = QuadroBoard(
        SqliteBoardBackend(":memory:"),
        profile_resolver={"work": profile},
    )
    network.register_endpoint(board_url, board.handle_request)

    def _req(intent: str, payload: dict) -> dict:
        resp = network.request(
            board_url, A2ARequest(intent=intent, payload=payload).to_dict()
        )
        assert resp["ok"], resp.get("error")
        return resp["result"]

    _req(
        "board.register_agent",
        {
            "agent_id": "worker_1",
            "name": "Worker",
            "url": "a2a://worker_1",
            "version": "1",
            "description": "test worker",
            "capabilities": ["work"],
        },
    )
    task_id = _req("board.post_task", {"task_type": "work", "label": "do work"})[
        "task"
    ]["task_id"]
    _req(
        "board.update_task",
        {
            "task_id": task_id,
            "to_status": "IN_PROGRESS",
            "assigned_to": "worker_1",
        },
    )
    return network, board_url, task_id


def test_stale_task_detected_and_transitioned() -> None:
    network, board_url, task_id = _make_env()
    # No heartbeat posted → heartbeat_at is None → stale at any timeout
    ombudsman = Ombudsman(
        network=network, board_url=board_url, heartbeat_timeout_seconds=300
    )

    count = ombudsman.nudge()

    assert count == 1

    task_resp = network.request(
        board_url,
        A2ARequest(intent="board.get_task", payload={"task_id": task_id}).to_dict(),
    )
    assert task_resp["ok"]
    assert task_resp["result"]["task"]["status"] == TaskStatus.STALE.value

    events = network.request(
        board_url,
        A2ARequest(
            intent="board.stream_events", payload={"since_sequence": 0}
        ).to_dict(),
    )["result"]["events"]
    stale_events = [e for e in events if e["event_type"] == "task_stale"]
    assert len(stale_events) == 1
    assert stale_events[0]["task_id"] == task_id


def test_recent_heartbeat_not_stale() -> None:
    network, board_url, task_id = _make_env()
    # Post a heartbeat right now — it is well within the 300-second window
    network.request(
        board_url,
        A2ARequest(
            intent="board.post_agent_heartbeat",
            payload={"agent_id": "worker_1", "task_id": task_id},
        ).to_dict(),
    )

    ombudsman = Ombudsman(
        network=network, board_url=board_url, heartbeat_timeout_seconds=300
    )
    count = ombudsman.nudge()

    assert count == 0

    task_resp = network.request(
        board_url,
        A2ARequest(intent="board.get_task", payload={"task_id": task_id}).to_dict(),
    )
    assert task_resp["result"]["task"]["status"] == TaskStatus.IN_PROGRESS.value


def test_ombudsman_triggers_chief_reassignment() -> None:
    network, board_url, task_id = _make_env(profile="fast")

    # Register a second worker so there is an idle agent for reassignment
    network.request(
        board_url,
        A2ARequest(
            intent="board.register_agent",
            payload={
                "agent_id": "worker_2",
                "name": "Worker2",
                "url": "a2a://worker_2",
                "version": "1",
                "description": "standby",
                "capabilities": ["work"],
            },
        ).to_dict(),
    )

    ombudsman = Ombudsman(
        network=network, board_url=board_url, heartbeat_timeout_seconds=300
    )
    staled = ombudsman.nudge()
    assert staled == 1

    # Chief processes the task_stale event \u2192 reassigns to UNASSIGNED
    chief = ChiefAgent(network=network, board_url=board_url)
    chief.nudge()

    task_resp = network.request(
        board_url,
        A2ARequest(intent="board.get_task", payload={"task_id": task_id}).to_dict(),
    )
    # After ombudsman + chief nudge, task must have left STALE (reassigned to UNASSIGNED)
    final_status = task_resp["result"]["task"]["status"]
    assert final_status == TaskStatus.UNASSIGNED.value

    events = network.request(
        board_url,
        A2ARequest(
            intent="board.stream_events", payload={"since_sequence": 0}
        ).to_dict(),
    )["result"]["events"]
    reassign_events = [e for e in events if e["event_type"] == "task_reassigned"]
    assert len(reassign_events) >= 1


def test_non_in_progress_tasks_ignored() -> None:
    network = LocalA2ANetwork()
    board_url = "a2a://board"
    board = QuadroBoard(
        SqliteBoardBackend(":memory:"),
        profile_resolver={"work": "review_required"},
    )
    network.register_endpoint(board_url, board.handle_request)

    def _req(intent: str, payload: dict) -> dict:
        resp = network.request(
            board_url, A2ARequest(intent=intent, payload=payload).to_dict()
        )
        assert resp["ok"], resp.get("error")
        return resp["result"]

    # Create tasks in various non-IN_PROGRESS states
    t_unassigned = _req(
        "board.post_task", {"task_type": "work", "label": "unassigned"}
    )["task"]["task_id"]

    t_pending = _req(
        "board.post_task", {"task_type": "work", "label": "pending review"}
    )["task"]["task_id"]
    _req("board.update_task", {"task_id": t_pending, "to_status": "IN_PROGRESS"})
    _req("board.update_task", {"task_id": t_pending, "to_status": "PENDING_REVIEW"})

    t_complete = _req("board.post_task", {"task_type": "work", "label": "completed"})[
        "task"
    ]["task_id"]
    _req(
        "board.register_agent",
        {
            "agent_id": "ag1",
            "name": "Ag",
            "url": "a2a://ag1",
            "version": "1",
            "description": "ag",
            "capabilities": ["work"],
        },
    )
    _req("board.update_task", {"task_id": t_complete, "to_status": "IN_PROGRESS"})
    _req("board.update_task", {"task_id": t_complete, "to_status": "PENDING_REVIEW"})
    _req("board.update_task", {"task_id": t_complete, "to_status": "IN_PROGRESS"})
    _req("board.update_task", {"task_id": t_complete, "to_status": "APPROVED"})
    _req("board.update_task", {"task_id": t_complete, "to_status": "COMPLETE"})

    ombudsman = Ombudsman(
        network=network, board_url=board_url, heartbeat_timeout_seconds=300
    )
    count = ombudsman.nudge()

    assert count == 0

    for tid, expected in [
        (t_unassigned, TaskStatus.UNASSIGNED.value),
        (t_pending, TaskStatus.PENDING_REVIEW.value),
        (t_complete, TaskStatus.COMPLETE.value),
    ]:
        resp = network.request(
            board_url,
            A2ARequest(intent="board.get_task", payload={"task_id": tid}).to_dict(),
        )
        assert (
            resp["result"]["task"]["status"] == expected
        ), f"task {tid} changed unexpectedly"

from __future__ import annotations

from quadro.a2a.contracts import A2ARequest
from quadro.a2a.dispatch import LocalA2ANetwork
from quadro.board.backends.sqlite import SqliteBoardBackend
from quadro.board.board import QuadroBoard


def _make_board() -> tuple[LocalA2ANetwork, str]:
    network = LocalA2ANetwork()
    board_url = "a2a://board"
    board = QuadroBoard(
        SqliteBoardBackend(":memory:"),
        profile_resolver={"task": "fast"},
    )
    network.register_endpoint(board_url, board.handle_request)
    return network, board_url


def _req(network: LocalA2ANetwork, url: str, intent: str, payload: dict) -> dict:
    resp = network.request(url, A2ARequest(intent=intent, payload=payload).to_dict())
    assert resp["ok"], resp.get("error")
    return resp["result"]


def test_get_task_history_returns_only_that_tasks_events() -> None:
    network, url = _make_board()

    t1 = _req(network, url, "board.post_task", {"task_type": "task", "label": "first"})[
        "task"
    ]["task_id"]
    t2 = _req(
        network, url, "board.post_task", {"task_type": "task", "label": "second"}
    )["task"]["task_id"]

    _req(network, url, "board.update_task", {"task_id": t1, "to_status": "IN_PROGRESS"})
    _req(network, url, "board.update_task", {"task_id": t2, "to_status": "IN_PROGRESS"})
    _req(network, url, "board.update_task", {"task_id": t1, "to_status": "COMPLETE"})

    result = _req(network, url, "board.get_task_history", {"task_id": t1})

    assert result["task_id"] == t1
    assert all(e["task_id"] == t1 for e in result["events"])
    seq_ids = [e["sequence_id"] for e in result["events"]]
    assert seq_ids == sorted(seq_ids)
    assert len(result["events"]) >= 2  # task_posted + task_assigned + task_completed


def test_get_task_history_includes_heartbeats() -> None:
    network, url = _make_board()

    agent_payload = {
        "agent_id": "agent_hb",
        "name": "HB",
        "url": "a2a://hb",
        "version": "1",
        "description": "hb agent",
        "capabilities": ["task"],
    }
    _req(network, url, "board.register_agent", agent_payload)

    tid = _req(
        network, url, "board.post_task", {"task_type": "task", "label": "hb test"}
    )["task"]["task_id"]
    _req(
        network,
        url,
        "board.update_task",
        {"task_id": tid, "to_status": "IN_PROGRESS", "assigned_to": "agent_hb"},
    )
    _req(
        network,
        url,
        "board.post_agent_heartbeat",
        {"agent_id": "agent_hb", "task_id": tid},
    )

    result = _req(network, url, "board.get_task_history", {"task_id": tid})

    event_types = [e["event_type"] for e in result["events"]]
    assert "task_heartbeat" in event_types


def test_get_agent_activity_returns_only_that_agents_events() -> None:
    network, url = _make_board()

    for aid in ("agent_alpha", "agent_beta"):
        _req(
            network,
            url,
            "board.register_agent",
            {
                "agent_id": aid,
                "name": aid,
                "url": f"a2a://{aid}",
                "version": "1",
                "description": aid,
                "capabilities": ["task"],
            },
        )

    t1 = _req(
        network, url, "board.post_task", {"task_type": "task", "label": "alpha task"}
    )["task"]["task_id"]
    t2 = _req(
        network, url, "board.post_task", {"task_type": "task", "label": "beta task"}
    )["task"]["task_id"]
    _req(
        network,
        url,
        "board.update_task",
        {"task_id": t1, "to_status": "IN_PROGRESS", "assigned_to": "agent_alpha"},
    )
    _req(
        network,
        url,
        "board.update_task",
        {"task_id": t2, "to_status": "IN_PROGRESS", "assigned_to": "agent_beta"},
    )

    result = _req(network, url, "board.get_agent_activity", {"agent_id": "agent_alpha"})

    assert result["agent_id"] == "agent_alpha"
    assert all(e["agent_id"] == "agent_alpha" for e in result["events"])
    assert len(result["events"]) >= 1


def test_get_task_history_empty_for_unknown_task() -> None:
    network, url = _make_board()

    result = _req(
        network, url, "board.get_task_history", {"task_id": "no-such-task-id"}
    )

    assert result["task_id"] == "no-such-task-id"
    assert result["events"] == []

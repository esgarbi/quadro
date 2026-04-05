from __future__ import annotations

from quadro import LocalA2ANetwork, QuadroBoard
from quadro.a2a.contracts import A2ARequest
from quadro.board.backends.sqlite import SqliteBoardBackend
from quadro.board.state_machine import lifecycle
from quadro.ombudsman import Ombudsman

CUSTOM_PROFILE = lifecycle(
    [
        ("UNASSIGNED", "working"),
        ("working", "idea_ready"),
        ("idea_ready", "done"),
    ]
)


def _make_env() -> tuple[LocalA2ANetwork, str]:
    network = LocalA2ANetwork()
    board_url = "a2a://board"
    board = QuadroBoard(
        SqliteBoardBackend(":memory:"),
        profile_resolver={"job": "job"},
        custom_profiles={"job": CUSTOM_PROFILE},
    )
    network.register_endpoint(board_url, board.handle_request)
    return network, board_url


def _req(network: LocalA2ANetwork, url: str, intent: str, payload: dict) -> dict:
    resp = network.request(url, A2ARequest(intent=intent, payload=payload).to_dict())
    assert resp["ok"], resp.get("error")
    return resp["result"]


# ── 1. Ombudsman detects stuck custom status ──────────────────────────────────


def test_ombudsman_detects_stuck_custom_status() -> None:
    network, board_url = _make_env()

    _req(
        network,
        board_url,
        "board.register_agent",
        {
            "agent_id": "w1",
            "name": "W1",
            "url": "a2a://w1",
            "version": "1",
            "description": "worker",
            "capabilities": ["work"],
        },
    )

    task_id = _req(
        network,
        board_url,
        "board.post_task",
        {
            "task_type": "job",
            "label": "test",
        },
    )["task"]["task_id"]

    _req(
        network,
        board_url,
        "board.update_task",
        {
            "task_id": task_id,
            "to_status": "working",
            "assigned_to": "w1",
        },
    )
    # No heartbeat posted → heartbeat_at is None → always stale

    ombudsman = Ombudsman(
        network=network,
        board_url=board_url,
        heartbeat_timeout_seconds=60,
        working_statuses={"working"},
    )
    count = ombudsman.nudge()

    assert count == 1

    task = _req(network, board_url, "board.get_task", {"task_id": task_id})["task"]
    assert task["status"] == "HUMAN_REVIEW"


# ── 2. Ombudsman ignores non-working custom status ────────────────────────────


def test_ombudsman_ignores_non_working_custom_status() -> None:
    network, board_url = _make_env()

    _req(
        network,
        board_url,
        "board.register_agent",
        {
            "agent_id": "w1",
            "name": "W1",
            "url": "a2a://w1",
            "version": "1",
            "description": "worker",
            "capabilities": ["work"],
        },
    )

    task_id = _req(
        network,
        board_url,
        "board.post_task",
        {
            "task_type": "job",
            "label": "test",
        },
    )["task"]["task_id"]

    _req(
        network,
        board_url,
        "board.update_task",
        {
            "task_id": task_id,
            "to_status": "working",
            "assigned_to": "w1",
        },
    )
    _req(
        network,
        board_url,
        "board.update_task",
        {
            "task_id": task_id,
            "to_status": "idea_ready",
        },
    )
    # Task is in "idea_ready" — a pending/ready status, not in working_statuses

    ombudsman = Ombudsman(
        network=network,
        board_url=board_url,
        heartbeat_timeout_seconds=60,
        working_statuses={"working"},
    )
    count = ombudsman.nudge()

    assert count == 0

    task = _req(network, board_url, "board.get_task", {"task_id": task_id})["task"]
    assert task["status"] == "idea_ready"

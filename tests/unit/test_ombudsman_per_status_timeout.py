from __future__ import annotations

from datetime import timedelta

from quadro import LocalA2ANetwork, QuadroBoard
from quadro.a2a.contracts import A2ARequest
from quadro.board.backends.sqlite import SqliteBoardBackend
from quadro.board.records import utc_now
from quadro.board.state_machine import lifecycle
from quadro.ombudsman import Ombudsman

CUSTOM_PROFILE = lifecycle(
    [
        ("UNASSIGNED", "validating"),
        ("validating", "validated"),
        ("UNASSIGNED", "procuring"),
        ("procuring", "procured"),
        ("validated", "done"),
        ("procured", "done"),
    ]
)


def _make_env() -> tuple[LocalA2ANetwork, str, SqliteBoardBackend]:
    network = LocalA2ANetwork()
    board_url = "a2a://board"
    backend = SqliteBoardBackend(":memory:")
    board = QuadroBoard(
        backend,
        profile_resolver={"order": "order"},
        custom_profiles={"order": CUSTOM_PROFILE},
    )
    network.register_endpoint(board_url, board.handle_request)
    return network, board_url, backend


def _req(network: LocalA2ANetwork, url: str, intent: str, payload: dict) -> dict:
    resp = network.request(url, A2ARequest(intent=intent, payload=payload).to_dict())
    assert resp["ok"], resp.get("error")
    return resp["result"]


def _backdate_heartbeat(
    backend: SqliteBoardBackend, task_id: str, seconds_ago: int
) -> None:
    """Set heartbeat_at to a specific number of seconds in the past."""
    past = utc_now() - timedelta(seconds=seconds_ago)
    backend._conn.execute(
        "UPDATE tasks SET heartbeat_at=? WHERE task_id=?",
        (past.isoformat(), task_id),
    )
    backend._conn.commit()


def _setup_task_in_status(
    network: LocalA2ANetwork,
    board_url: str,
    backend: SqliteBoardBackend,
    status: str,
    heartbeat_seconds_ago: int,
) -> str:
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
            "task_type": "order",
            "label": "test",
        },
    )["task"]["task_id"]
    _req(
        network,
        board_url,
        "board.update_task",
        {
            "task_id": task_id,
            "to_status": status,
            "assigned_to": "w1",
        },
    )
    _req(
        network,
        board_url,
        "board.post_agent_heartbeat",
        {
            "agent_id": "w1",
            "task_id": task_id,
        },
    )
    _backdate_heartbeat(backend, task_id, heartbeat_seconds_ago)
    return task_id


# ── 1. Per-status timeout fires before global would ──────────────────────────


def test_ombudsman_uses_per_status_timeout_not_global() -> None:
    network, board_url, backend = _make_env()

    task_id = _setup_task_in_status(network, board_url, backend, "validating", 90)

    ombudsman = Ombudsman(
        network=network,
        board_url=board_url,
        heartbeat_timeout_seconds=300,
        working_statuses={"validating", "procuring"},
        status_timeouts={"validating": 60},
    )
    count = ombudsman.nudge()

    assert count == 1
    task = _req(network, board_url, "board.get_task", {"task_id": task_id})["task"]
    assert task["status"] == "HUMAN_REVIEW"


# ── 2. Unspecified status falls back to global timeout ────────────────────────


def test_ombudsman_falls_back_to_global_for_unspecified_status() -> None:
    network, board_url, backend = _make_env()

    task_id = _setup_task_in_status(network, board_url, backend, "procuring", 200)

    ombudsman = Ombudsman(
        network=network,
        board_url=board_url,
        heartbeat_timeout_seconds=300,
        working_statuses={"validating", "procuring"},
        status_timeouts={"validating": 60},
    )
    count = ombudsman.nudge()

    assert count == 0
    task = _req(network, board_url, "board.get_task", {"task_id": task_id})["task"]
    assert task["status"] == "procuring"


# ── 3. Notes include the timeout value ────────────────────────────────────────


def test_ombudsman_notes_include_timeout_value() -> None:
    network, board_url, backend = _make_env()

    task_id = _setup_task_in_status(network, board_url, backend, "validating", 90)

    ombudsman = Ombudsman(
        network=network,
        board_url=board_url,
        heartbeat_timeout_seconds=300,
        working_statuses={"validating"},
        status_timeouts={"validating": 60},
    )
    ombudsman.nudge()

    task = _req(network, board_url, "board.get_task", {"task_id": task_id})["task"]
    assert any("timeout=60s" in note for note in task["notes"])

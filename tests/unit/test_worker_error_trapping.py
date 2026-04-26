from __future__ import annotations

from quadro import BoardClient, LocalA2ANetwork, QuadroBoard, WorkerAgent
from quadro.a2a.contracts import A2ARequest
from quadro.board.backends.sqlite import SqliteBoardBackend
from quadro.board.state_machine import lifecycle

CUSTOM_PROFILE = lifecycle(
    [
        ("UNASSIGNED", "working"),
        ("working", "done"),
    ]
)


def _make_env() -> tuple[LocalA2ANetwork, str, BoardClient]:
    network = LocalA2ANetwork()
    board_url = "a2a://board"
    board = QuadroBoard(
        SqliteBoardBackend(":memory:"),
        profile_resolver={"job": "job"},
        custom_profiles={"job": CUSTOM_PROFILE},
    )
    network.register_endpoint(board_url, board.handle_request)
    bc = BoardClient(network, board_url)
    return network, board_url, bc


def _req(network: LocalA2ANetwork, url: str, intent: str, payload: dict) -> dict:
    resp = network.request(url, A2ARequest(intent=intent, payload=payload).to_dict())
    assert resp["ok"], resp.get("error")
    return resp["result"]


# ── 1. Worker fails task on execute_fn error ──────────────────────────────────


def test_worker_fails_task_on_execute_error() -> None:
    network, board_url, bc = _make_env()

    def _boom(ctx: dict, board_fn) -> str:
        raise RuntimeError("boom")

    worker = WorkerAgent(
        agent_id="w1",
        name="W1",
        capabilities=["work"],
        url="a2a://w1",
        board_url=board_url,
        network=network,
        execute_fn=_boom,
    )
    worker.register()

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

    resp = network.request(
        "a2a://w1",
        A2ARequest(
            intent="worker.execute_task",
            payload={"task_id": task_id},
        ).to_dict(),
    )
    assert not resp["ok"]

    task = _req(network, board_url, "board.get_task", {"task_id": task_id})["task"]
    assert task["status"] == "HUMAN_REVIEW"

    state = _req(network, board_url, "board.get_full_state", {})
    agent = next(a for a in state["agents"] if a["agent_id"] == "w1")
    assert agent["status"] == "IDLE"


# ── 2. Worker wakes chief on failure ──────────────────────────────────────────


def test_worker_wakes_chief_on_failure() -> None:
    network, board_url, bc = _make_env()

    wake_calls: list[dict] = []

    def mock_chief_handler(envelope: dict) -> dict:
        wake_calls.append(envelope)
        return {
            "ok": True,
            "result": {},
            "error": None,
            "request_id": envelope.get("request_id", "x"),
        }

    network.register_endpoint("a2a://chief", mock_chief_handler)

    def _boom(ctx: dict, board_fn) -> str:
        raise RuntimeError("boom")

    worker = WorkerAgent(
        agent_id="w1",
        name="W1",
        capabilities=["work"],
        url="a2a://w1",
        board_url=board_url,
        network=network,
        execute_fn=_boom,
        chief_url="a2a://chief",
    )
    worker.register()

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

    network.request(
        "a2a://w1",
        A2ARequest(
            intent="worker.execute_task",
            payload={"task_id": task_id},
        ).to_dict(),
    )

    assert len(wake_calls) >= 1


# ── 3. Worker stores error in notes ───────────────────────────────────────────


def test_worker_stores_error_in_notes() -> None:
    network, board_url, bc = _make_env()

    def _fail(ctx: dict, board_fn) -> str:
        raise ValueError("test error message")

    worker = WorkerAgent(
        agent_id="w1",
        name="W1",
        capabilities=["work"],
        url="a2a://w1",
        board_url=board_url,
        network=network,
        execute_fn=_fail,
    )
    worker.register()

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

    network.request(
        "a2a://w1",
        A2ARequest(
            intent="worker.execute_task",
            payload={"task_id": task_id},
        ).to_dict(),
    )

    task = _req(network, board_url, "board.get_task", {"task_id": task_id})["task"]
    assert task["status"] == "HUMAN_REVIEW"
    assert any("test error message" in n for n in task.get("notes", []))

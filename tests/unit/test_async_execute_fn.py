from __future__ import annotations

import asyncio

from quadro import LocalA2ANetwork, QuadroBoard, WorkerAgent
from quadro.a2a.contracts import A2ARequest
from quadro.board.backends.sqlite import SqliteBoardBackend


def _make_env() -> tuple[LocalA2ANetwork, str, str]:
    """Returns (network, board_url, task_id) with one IN_PROGRESS task."""
    network = LocalA2ANetwork()
    board_url = "a2a://board"
    board = QuadroBoard(
        SqliteBoardBackend(":memory:"),
        profile_resolver={"work": "fast"},
    )
    network.register_endpoint(board_url, board.handle_request)

    def _req(intent: str, payload: dict) -> dict:
        resp = network.request(
            board_url, A2ARequest(intent=intent, payload=payload).to_dict()
        )
        assert resp["ok"], resp.get("error")
        return resp["result"]

    task_id = _req("board.post_task", {"task_type": "work", "label": "test"})["task"][
        "task_id"
    ]
    _req("board.update_task", {"task_id": task_id, "to_status": "IN_PROGRESS"})
    return network, board_url, task_id


def test_async_execute_fn_is_awaited() -> None:
    """An async execute_fn (returns a coroutine) is transparently awaited."""
    network, board_url, task_id = _make_env()

    async def async_fn(ctx: dict, board) -> str:
        await asyncio.sleep(0)  # yield to event loop at least once
        return "async-output"

    worker = WorkerAgent(
        agent_id="w1",
        name="W1",
        capabilities=["work"],
        url="a2a://w1",
        board_url=board_url,
        network=network,
        execute_fn=async_fn,
    )
    worker.register()

    resp = network.request(
        "a2a://w1",
        A2ARequest(
            intent="worker.execute_task", payload={"task_id": task_id}
        ).to_dict(),
    )
    assert resp["ok"], resp.get("error")

    task = network.request(
        board_url,
        A2ARequest(intent="board.get_task", payload={"task_id": task_id}).to_dict(),
    )["result"]["task"]
    assert task["status"] == "COMPLETE"
    assert task["output"] == "async-output"


def test_sync_execute_fn_unchanged() -> None:
    """A plain (sync) execute_fn continues to work exactly as before."""
    network, board_url, task_id = _make_env()

    worker = WorkerAgent(
        agent_id="w2",
        name="W2",
        capabilities=["work"],
        url="a2a://w2",
        board_url=board_url,
        network=network,
        execute_fn=lambda ctx, _: "sync-output",
    )
    worker.register()

    resp = network.request(
        "a2a://w2",
        A2ARequest(
            intent="worker.execute_task", payload={"task_id": task_id}
        ).to_dict(),
    )
    assert resp["ok"], resp.get("error")

    task = network.request(
        board_url,
        A2ARequest(intent="board.get_task", payload={"task_id": task_id}).to_dict(),
    )["result"]["task"]
    assert task["status"] == "COMPLETE"
    assert task["output"] == "sync-output"

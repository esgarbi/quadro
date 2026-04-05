from __future__ import annotations

from quadro.a2a.contracts import A2ARequest
from quadro.a2a.dispatch import LocalA2ANetwork
from quadro.board.backends.sqlite import SqliteBoardBackend
from quadro.board.board import QuadroBoard
from quadro.agents.worker import WorkerAgent


def _make_env(execute_fn):
    network = LocalA2ANetwork()
    board_url = "a2a://board"
    board = QuadroBoard(
        SqliteBoardBackend(":memory:"), profile_resolver={"test": "fast"}
    )
    network.register_endpoint(board_url, board.handle_request)

    worker = WorkerAgent(
        agent_id="worker_1",
        name="TestWorker",
        capabilities=["test"],
        url="a2a://workers/worker_1",
        board_url=board_url,
        network=network,
        execute_fn=execute_fn,
    )
    worker.register()

    resp = network.request(
        board_url,
        A2ARequest(
            intent="board.post_task",
            payload={"task_type": "test", "label": "test task"},
        ).to_dict(),
    )
    assert resp["ok"]
    task_id = resp["result"]["task"]["task_id"]

    resp = network.request(
        board_url,
        A2ARequest(
            intent="board.update_task",
            payload={
                "task_id": task_id,
                "to_status": "IN_PROGRESS",
                "assigned_to": "worker_1",
            },
        ).to_dict(),
    )
    assert resp["ok"]

    return network, board_url, task_id


def test_operational_worker_can_call_board_intents() -> None:
    board_call_results: list[dict] = []

    def execute_fn(ctx: dict, board: object) -> str:
        result = board("board.put_data", {"key": "test-key", "value": {"x": 42}})
        board_call_results.append(result)
        read_back = board("board.get_data", {"key": "test-key"})
        board_call_results.append(read_back)
        return "done"

    network, board_url, task_id = _make_env(execute_fn)
    network.request(
        "a2a://workers/worker_1",
        A2ARequest(
            intent="worker.execute_task", payload={"task_id": task_id}
        ).to_dict(),
    )

    assert len(board_call_results) == 2
    assert board_call_results[1]["value"] == {"x": 42}


def test_simple_worker_ignores_board_fn() -> None:
    network, board_url, task_id = _make_env(
        lambda ctx, _: f"output for {ctx['payload']['task']['task_id']}"
    )
    resp = network.request(
        "a2a://workers/worker_1",
        A2ARequest(
            intent="worker.execute_task", payload={"task_id": task_id}
        ).to_dict(),
    )
    assert resp["ok"]
    task_resp = network.request(
        board_url,
        A2ARequest(intent="board.get_task", payload={"task_id": task_id}).to_dict(),
    )
    assert task_resp["ok"]
    assert task_resp["result"]["task"]["status"] == "COMPLETE"

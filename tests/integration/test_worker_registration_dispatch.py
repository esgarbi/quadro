import time

from quadro.a2a.contracts import A2ARequest
from quadro.a2a.dispatch import LocalA2ANetwork
from quadro.agents.chief import ChiefAgent
from quadro.agents.worker import WorkerAgent
from quadro.board.board import QuadroBoard
from quadro.board.backends.sqlite import SqliteBoardBackend


def _setup_fast_system() -> tuple[LocalA2ANetwork, str, ChiefAgent]:
    network = LocalA2ANetwork()
    board_url = "a2a://board"
    board = QuadroBoard(SqliteBoardBackend(), profile_resolver={"research": "fast"})
    network.register_endpoint(board_url, board.handle_request)

    worker = WorkerAgent(
        agent_id="researcher_1",
        name="Researcher",
        capabilities=["research"],
        url="a2a://workers/researcher_1",
        board_url=board_url,
        network=network,
        execute_fn=lambda ctx, _: f"research-output:{ctx['payload']['task']['label']}",
    )
    worker.register()

    chief = ChiefAgent(network=network, board_url=board_url)
    return network, board_url, chief


def test_worker_registration_and_dispatch() -> None:
    network, board_url, chief = _setup_fast_system()
    post = network.request(
        board_url,
        A2ARequest(
            intent="board.post_task",
            payload={"task_type": "research", "label": "water crisis"},
        ).to_dict(),
    )
    assert post["ok"]
    task_id = post["result"]["task"]["task_id"]

    # Poll until the task completes or the deadline is reached.
    # Workers now run in background daemon threads, so we give them time to finish.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        chief.nudge()
        task_state = network.request(
            board_url,
            A2ARequest(intent="board.get_task", payload={"task_id": task_id}).to_dict(),
        )
        assert task_state["ok"]
        if task_state["result"]["task"]["status"] == "COMPLETE":
            break
        time.sleep(0.02)

    task = task_state["result"]["task"]
    assert task["status"] == "COMPLETE"
    assert task["heartbeat_at"] is not None

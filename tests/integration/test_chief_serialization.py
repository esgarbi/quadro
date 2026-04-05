import threading

from quadro.a2a.contracts import A2ARequest
from quadro.a2a.dispatch import LocalA2ANetwork
from quadro.agents.chief import ChiefAgent
from quadro.board.board import QuadroBoard
from quadro.board.backends.sqlite import SqliteBoardBackend


def test_chief_serialized_loop_under_burst() -> None:
    network = LocalA2ANetwork()
    board_url = "a2a://board"
    board = QuadroBoard(SqliteBoardBackend(), profile_resolver={"research": "fast"})
    network.register_endpoint(board_url, board.handle_request)

    chief = ChiefAgent(network=network, board_url=board_url)

    # Seed several events.
    for _ in range(3):
        network.request(
            board_url,
            A2ARequest(
                intent="board.post_task",
                payload={"task_type": "research", "label": "seed"},
            ).to_dict(),
        )

    threads = [threading.Thread(target=chief.nudge) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert chief.max_concurrent_loops == 1

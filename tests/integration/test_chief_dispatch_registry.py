from quadro.a2a.contracts import A2ARequest
from quadro.a2a.dispatch import LocalA2ANetwork
from quadro.agents.chief import ChiefAgent
from quadro.board.board import QuadroBoard
from quadro.board.backends.sqlite import SqliteBoardBackend


def test_chief_swallows_dispatch_error_when_url_not_registered() -> None:
    """
    When an agent is registered on the board but its URL is not registered on
    the network, _dispatch_worker logs a warning and does not raise.
    The task is left in IN_PROGRESS (board update succeeded; only the dispatch failed).
    """
    network = LocalA2ANetwork()
    board_url = "a2a://board"
    board = QuadroBoard(SqliteBoardBackend(), profile_resolver={"research": "fast"})
    network.register_endpoint(board_url, board.handle_request)

    network.request(
        board_url,
        A2ARequest(
            intent="board.register_agent",
            payload={
                "agent_id": "a1",
                "name": "A",
                "url": "http://orphan-worker",
                "version": "1",
                "description": "d",
                "capabilities": ["research"],
            },
        ).to_dict(),
    )
    network.request(
        board_url,
        A2ARequest(
            intent="board.post_task", payload={"task_type": "research", "label": "x"}
        ).to_dict(),
    )
    chief = ChiefAgent(network=network, board_url=board_url)

    # Must NOT raise — dispatch error is logged as a warning
    chief.nudge()

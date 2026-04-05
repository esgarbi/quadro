from quadro.a2a.contracts import A2ARequest
from quadro.a2a.dispatch import LocalA2ANetwork
from quadro.agents.chief import ChiefAgent
from quadro.board.board import QuadroBoard
from quadro.board.backends.sqlite import SqliteBoardBackend


def test_chief_policy_receives_snapshot_hash_from_hydration() -> None:
    network = LocalA2ANetwork()
    board_url = "a2a://board"
    board = QuadroBoard(SqliteBoardBackend())
    network.register_endpoint(board_url, board.handle_request)

    hashes: list[str] = []

    def policy(ctx: dict) -> None:
        assert "snapshot_hash" in ctx
        assert len(ctx["snapshot_hash"]) == 64
        hashes.append(ctx["snapshot_hash"])

    chief = ChiefAgent(network=network, board_url=board_url, policy=policy)
    network.request(
        board_url,
        A2ARequest(
            intent="board.post_task", payload={"task_type": "draft", "label": "b"}
        ).to_dict(),
    )
    chief.nudge()
    assert len(hashes) == 1

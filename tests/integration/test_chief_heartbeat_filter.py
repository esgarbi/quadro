from quadro.a2a.contracts import A2ARequest, A2AResponse
from quadro.a2a.dispatch import LocalA2ANetwork
from quadro.agents.chief import ChiefAgent
from quadro.board.board import QuadroBoard
from quadro.board.backends.sqlite import SqliteBoardBackend


def test_chief_skips_heartbeat_for_coordination_policy() -> None:
    network = LocalA2ANetwork()
    board_url = "a2a://board"
    board = QuadroBoard(SqliteBoardBackend(), profile_resolver={"research": "fast"})
    network.register_endpoint(board_url, board.handle_request)

    policy_calls: list[dict] = []

    def policy(ctx: dict) -> None:
        policy_calls.append(ctx)

    chief = ChiefAgent(network=network, board_url=board_url, policy=policy)

    network.register_endpoint(
        "http://a",
        lambda env: A2AResponse(
            request_id=env["request_id"], ok=True, result={}
        ).to_dict(),
    )

    network.request(
        board_url,
        A2ARequest(
            intent="board.register_agent",
            payload={
                "agent_id": "a1",
                "name": "A",
                "url": "http://a",
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
    task_id = network.request(
        board_url, A2ARequest(intent="board.get_full_state", payload={}).to_dict()
    )["result"]["tasks"][0]["task_id"]

    # Nudge 1: processes task_posted → chief assigns worker → task_assigned event created.
    chief.nudge()
    policy_calls.clear()

    # Nudge 2: processes task_assigned (cursor advances past it); task is already IN_PROGRESS
    # so no board writes occur, but cursor must advance before the heartbeat-only phase.
    chief.nudge()
    policy_calls.clear()

    # Assert the task is properly IN_PROGRESS before posting the heartbeat.
    task_state = network.request(
        board_url,
        A2ARequest(intent="board.get_task", payload={"task_id": task_id}).to_dict(),
    )
    assert task_state["ok"]
    assert task_state["result"]["task"]["status"] == "IN_PROGRESS"

    # Post a heartbeat — the only new event from here onward.
    network.request(
        board_url,
        A2ARequest(
            intent="board.post_agent_heartbeat",
            payload={"agent_id": "a1", "task_id": task_id},
        ).to_dict(),
    )

    # Nudge 3: only task_heartbeat is in the stream. In the reactive model the chief
    # always runs a full decision cycle — the policy IS called — but the task is
    # already IN_PROGRESS so routing makes no changes and the status is unchanged.
    chief.nudge()
    assert policy_calls, "Reactive model: policy is called on every nudge"
    task_after = network.request(
        board_url,
        A2ARequest(intent="board.get_task", payload={"task_id": task_id}).to_dict(),
    )
    assert task_after["result"]["task"]["status"] == "IN_PROGRESS", (
        "Task must not be re-routed"
    )

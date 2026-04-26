from quadro.a2a.contracts import A2ARequest
from quadro.a2a.dispatch import LocalA2ANetwork
from quadro.agents.chief import ChiefAgent
from quadro.agents.worker import WorkerAgent
from quadro.board.board import QuadroBoard
from quadro.board.backends.sqlite import SqliteBoardBackend


def _full_state(network: LocalA2ANetwork, board_url: str) -> dict:
    response = network.request(
        board_url, A2ARequest(intent="board.get_full_state", payload={}).to_dict()
    )
    assert response["ok"]
    return response["result"]


def test_revision_cycle_reaches_complete() -> None:
    """
    Full revision cycle:
    UNASSIGNED → IN_PROGRESS (writer) → PENDING_REVIEW
    → IN_PROGRESS (reviewer, rejects) → REVISION_NEEDED
    → IN_PROGRESS (writer, second attempt) → PENDING_REVIEW
    → IN_PROGRESS (reviewer, approves) → APPROVED → COMPLETE
    """
    network = LocalA2ANetwork()
    board_url = "a2a://board"
    board = QuadroBoard(
        SqliteBoardBackend(),
        profile_resolver={"draft": "review_required"},
    )
    network.register_endpoint(board_url, board.handle_request)

    writer = WorkerAgent(
        agent_id="writer_1",
        name="Writer",
        capabilities=["draft"],
        url="a2a://workers/writer_1",
        board_url=board_url,
        network=network,
        execute_fn=lambda ctx, _: f"draft-text:{ctx['payload']['task']['label']}",
    )
    reviewer_call_count = [0]

    def reviewer_fn(ctx: dict, _: object) -> str:
        reviewer_call_count[0] += 1
        return "REVISION_NEEDED" if reviewer_call_count[0] == 1 else "review-approved"

    reviewer = WorkerAgent(
        agent_id="reviewer_1",
        name="Reviewer",
        capabilities=["review"],
        url="a2a://workers/reviewer_1",
        board_url=board_url,
        network=network,
        execute_fn=reviewer_fn,
        reviewer_mode=True,
    )
    for worker in (writer, reviewer):
        worker.register()

    chief = ChiefAgent(network=network, board_url=board_url)

    start = network.request(
        board_url,
        A2ARequest(
            intent="board.post_task",
            payload={"task_type": "draft", "label": "revision test"},
        ).to_dict(),
    )
    assert start["ok"]
    task_id = start["result"]["task"]["task_id"]

    for _ in range(40):
        chief.nudge()
        task_state = network.request(
            board_url,
            A2ARequest(intent="board.get_task", payload={"task_id": task_id}).to_dict(),
        )
        if task_state["result"]["task"]["status"] == "COMPLETE":
            break

    task = network.request(
        board_url,
        A2ARequest(intent="board.get_task", payload={"task_id": task_id}).to_dict(),
    )["result"]["task"]
    assert task["status"] == "COMPLETE"
    assert task["assigned_to"] == "reviewer_1"

    events_resp = network.request(
        board_url,
        A2ARequest(
            intent="board.stream_events", payload={"since_sequence": 0}
        ).to_dict(),
    )
    events = events_resp["result"]["events"]

    draft_assignments = [
        e
        for e in events
        if e["event_type"] == "task_assigned" and e["task_id"] == task_id
    ]
    assert len(draft_assignments) >= 4, (
        f"Expected at least 4 task_assigned events for the draft task "
        f"(writer, reviewer, writer again, reviewer again); got {len(draft_assignments)}"
    )

    reviewed_events = [
        e
        for e in events
        if e["event_type"] == "task_reviewed" and e["task_id"] == task_id
    ]
    assert len(reviewed_events) >= 2, (
        f"Expected at least 2 task_reviewed events (rejection + approval); got {len(reviewed_events)}"
    )

    to_statuses = [e["to_status"] for e in reviewed_events]
    assert "REVISION_NEEDED" in to_statuses, (
        "Expected at least one rejection (REVISION_NEEDED)"
    )
    assert "APPROVED" in to_statuses, "Expected at least one approval (APPROVED)"

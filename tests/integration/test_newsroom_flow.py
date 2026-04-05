from quadro.a2a.contracts import A2ARequest
from quadro.a2a.dispatch import LocalA2ANetwork
from quadro.agents.chief import ChiefAgent
from quadro.agents.worker import WorkerAgent
from quadro.board.board import QuadroBoard
from quadro.board.backends.sqlite import SqliteBoardBackend
from quadro.board.client import BoardClient


def _state(network: LocalA2ANetwork, board_url: str) -> dict:
    response = network.request(
        board_url, A2ARequest(intent="board.get_full_state", payload={}).to_dict()
    )
    assert response["ok"]
    return response["result"]


def test_newsroom_cooperation_flow_reaches_complete() -> None:
    network = LocalA2ANetwork()
    board_url = "a2a://board"
    board = QuadroBoard(
        SqliteBoardBackend(),
        profile_resolver={
            "research": "fast",
            "draft": "review_required",
        },
    )
    network.register_endpoint(board_url, board.handle_request)

    researcher = WorkerAgent(
        agent_id="researcher_1",
        name="Researcher",
        capabilities=["research"],
        url="a2a://workers/researcher_1",
        board_url=board_url,
        network=network,
        execute_fn=lambda ctx, _: f"research-summary:{ctx['payload']['task']['label']}",
    )
    writer = WorkerAgent(
        agent_id="writer_1",
        name="Writer",
        capabilities=["draft"],
        url="a2a://workers/writer_1",
        board_url=board_url,
        network=network,
        execute_fn=lambda ctx, _: f"draft-text:{ctx['payload']['task']['label']}",
    )
    reviewer = WorkerAgent(
        agent_id="reviewer_1",
        name="Reviewer",
        capabilities=["review"],
        url="a2a://workers/reviewer_1",
        board_url=board_url,
        network=network,
        execute_fn=lambda ctx, _: "review-approved",
        reviewer_mode=True,
    )
    for worker in (researcher, writer, reviewer):
        worker.register()

    bc = BoardClient(network, board_url)

    def _chain_policy(ctx: dict) -> None:
        tasks = ctx["payload"]["tasks"]
        for task in tasks:
            if task["task_type"] != "research" or task["status"] != "COMPLETE":
                continue
            source_note = f"source_research_task={task['task_id']}"
            already_chained = any(
                t["task_type"] == "draft" and source_note in t.get("notes", [])
                for t in tasks
            )
            if not already_chained:
                bc.post_task(
                    "draft",
                    f"Draft article from: {task['label']}",
                    notes=[source_note],
                )

    chief = ChiefAgent(network=network, board_url=board_url, policy=_chain_policy)

    start = network.request(
        board_url,
        A2ARequest(
            intent="board.post_task",
            payload={"task_type": "research", "label": "Water crisis in Sao Paulo"},
        ).to_dict(),
    )
    assert start["ok"]

    for _ in range(20):
        processed = chief.nudge()
        current = _state(network, board_url)
        draft_tasks = [t for t in current["tasks"] if t["task_type"] == "draft"]
        if draft_tasks and all(t["status"] == "COMPLETE" for t in draft_tasks):
            break
        if processed == 0:
            continue

    final = _state(network, board_url)
    task_types = {task["task_type"] for task in final["tasks"]}
    assert {"research", "draft"} <= task_types

    draft = [task for task in final["tasks"] if task["task_type"] == "draft"][0]
    assert draft["status"] == "COMPLETE"

    events = network.request(
        board_url,
        A2ARequest(
            intent="board.stream_events", payload={"since_sequence": 0}
        ).to_dict(),
    )
    assert events["ok"]
    event_types = {event["event_type"] for event in events["result"]["events"]}
    assert "task_reviewed" in event_types

    draft_id = draft["task_id"]
    reviewer_assigned = [
        e
        for e in events["result"]["events"]
        if e["event_type"] == "task_assigned"
        and e["task_id"] == draft_id
        and e["agent_id"] == "reviewer_1"
    ]
    assert (
        reviewer_assigned
    ), "expected chief-driven reviewer assignment before approval"

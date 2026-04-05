from quadro.a2a.contracts import A2ARequest
from quadro.board.board import QuadroBoard
from quadro.board.backends.sqlite import SqliteBoardBackend


def test_valid_transition_emits_single_event() -> None:
    board = QuadroBoard(
        SqliteBoardBackend(), profile_resolver={"draft": "review_required"}
    )
    post = board.handle_request(
        A2ARequest(
            intent="board.post_task", payload={"task_type": "draft", "label": "x"}
        ).to_dict()
    )
    assert post["ok"]
    task_id = post["result"]["task"]["task_id"]

    update = board.handle_request(
        A2ARequest(
            intent="board.update_task",
            payload={
                "task_id": task_id,
                "to_status": "IN_PROGRESS",
                "assigned_to": "writer_1",
            },
        ).to_dict()
    )
    assert update["ok"]
    assert update["result"]["event"]["event_type"] == "task_assigned"

    events = board.handle_request(
        A2ARequest(
            intent="board.stream_events", payload={"since_sequence": 0}
        ).to_dict()
    )
    assert events["ok"]
    assert len(events["result"]["events"]) == 2


def test_illegal_transition_emits_no_event() -> None:
    board = QuadroBoard(
        SqliteBoardBackend(), profile_resolver={"draft": "review_required"}
    )
    post = board.handle_request(
        A2ARequest(
            intent="board.post_task", payload={"task_type": "draft", "label": "x"}
        ).to_dict()
    )
    task_id = post["result"]["task"]["task_id"]
    board.handle_request(
        A2ARequest(
            intent="board.update_task",
            payload={
                "task_id": task_id,
                "to_status": "IN_PROGRESS",
                "assigned_to": "writer_1",
            },
        ).to_dict()
    )
    before = board.handle_request(
        A2ARequest(
            intent="board.stream_events", payload={"since_sequence": 0}
        ).to_dict()
    )
    before_count = len(before["result"]["events"])

    illegal = board.handle_request(
        A2ARequest(
            intent="board.update_task",
            payload={"task_id": task_id, "to_status": "COMPLETE"},
        ).to_dict()
    )
    assert not illegal["ok"]

    after = board.handle_request(
        A2ARequest(
            intent="board.stream_events", payload={"since_sequence": 0}
        ).to_dict()
    )
    assert len(after["result"]["events"]) == before_count


def test_event_sequence_ids_strictly_increasing() -> None:
    board = QuadroBoard(
        SqliteBoardBackend(), profile_resolver={"draft": "review_required"}
    )
    board.handle_request(
        A2ARequest(
            intent="board.post_task", payload={"task_type": "draft", "label": "x"}
        ).to_dict()
    )
    tid = board.handle_request(
        A2ARequest(intent="board.get_full_state", payload={}).to_dict()
    )["result"]["tasks"][0]["task_id"]
    board.handle_request(
        A2ARequest(
            intent="board.update_task",
            payload={"task_id": tid, "to_status": "IN_PROGRESS", "assigned_to": "w1"},
        ).to_dict()
    )
    stream = board.handle_request(
        A2ARequest(
            intent="board.stream_events", payload={"since_sequence": 0}
        ).to_dict()
    )
    seq = [e["sequence_id"] for e in stream["result"]["events"]]
    assert seq == sorted(seq)
    assert len(seq) == len(set(seq))


def test_frozen_event_taxonomy_enforced() -> None:
    board = QuadroBoard(SqliteBoardBackend())
    try:
        board._append_event(  # type: ignore[attr-defined]
            event_type="not_allowed",
            task_id="task-1",
            agent_id=None,
            from_status=None,
            to_status=None,
            payload={},
        )
    except ValueError:
        pass
    else:
        raise AssertionError("Expected ValueError for invalid event taxonomy")

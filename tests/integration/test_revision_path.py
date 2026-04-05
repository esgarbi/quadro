from __future__ import annotations

from quadro.a2a.contracts import A2ARequest
from quadro.a2a.dispatch import LocalA2ANetwork
from quadro.board.board import QuadroBoard
from quadro.board.backends.sqlite import SqliteBoardBackend


def _request(
    network: LocalA2ANetwork, board_url: str, intent: str, payload: dict
) -> dict:
    resp = network.request(
        board_url, A2ARequest(intent=intent, payload=payload).to_dict()
    )
    assert resp["ok"], resp.get("error")
    return resp["result"]


def _get_task(network: LocalA2ANetwork, board_url: str, task_id: str) -> dict:
    return _request(network, board_url, "board.get_task", {"task_id": task_id})["task"]


def test_revision_path_assigned_to_audit_trail() -> None:
    """
    Walk a review_required task through the full revision cycle and verify
    assigned_to at every phase:

    UNASSIGNED
      → IN_PROGRESS   (writer_1 assigned)
      → PENDING_REVIEW
      → IN_PROGRESS   (reviewer_1 assigned; reviewer rejects → REVISION_NEEDED)
      → IN_PROGRESS   (writer_1 re-assigned)
      → PENDING_REVIEW
      → IN_PROGRESS   (reviewer_1 assigned again; reviewer approves → APPROVED)
      → COMPLETE
    """
    network = LocalA2ANetwork()
    board_url = "a2a://board"
    board = QuadroBoard(
        SqliteBoardBackend(),
        profile_resolver={"article": "review_required"},
    )
    network.register_endpoint(board_url, board.handle_request)

    # ── Post task ─────────────────────────────────────────────────────────
    result = _request(
        network,
        board_url,
        "board.post_task",
        {
            "task_type": "article",
            "label": "revision path audit test",
        },
    )
    task_id = result["task"]["task_id"]

    task = _get_task(network, board_url, task_id)
    assert task["status"] == "UNASSIGNED"
    assert task["assigned_to"] is None

    # ── 1. UNASSIGNED → IN_PROGRESS (writer_1) ───────────────────────────
    _request(
        network,
        board_url,
        "board.update_task",
        {
            "task_id": task_id,
            "to_status": "IN_PROGRESS",
            "assigned_to": "writer_1",
        },
    )
    task = _get_task(network, board_url, task_id)
    assert task["status"] == "IN_PROGRESS"
    assert task["assigned_to"] == "writer_1"

    # ── 2. IN_PROGRESS → PENDING_REVIEW (writer posts result) ────────────
    _request(
        network,
        board_url,
        "worker.post_result",
        {
            "task_id": task_id,
            "agent_id": "writer_1",
            "output": "first draft",
        },
    )
    task = _get_task(network, board_url, task_id)
    assert task["status"] == "PENDING_REVIEW"
    assert task["assigned_to"] == "writer_1"

    # ── 3. PENDING_REVIEW → IN_PROGRESS (reviewer_1 assigned) ────────────
    _request(
        network,
        board_url,
        "board.update_task",
        {
            "task_id": task_id,
            "to_status": "IN_PROGRESS",
            "assigned_to": "reviewer_1",
        },
    )
    task = _get_task(network, board_url, task_id)
    assert task["status"] == "IN_PROGRESS"
    assert task["assigned_to"] == "reviewer_1"

    # ── 4. IN_PROGRESS → REVISION_NEEDED (reviewer rejects) ─────────────
    _request(
        network,
        board_url,
        "board.update_task",
        {
            "task_id": task_id,
            "to_status": "REVISION_NEEDED",
            "assigned_to": "reviewer_1",
        },
    )
    task = _get_task(network, board_url, task_id)
    assert task["status"] == "REVISION_NEEDED"
    assert task["assigned_to"] == "reviewer_1"

    # ── 5. REVISION_NEEDED → IN_PROGRESS (writer_1 re-assigned) ─────────
    _request(
        network,
        board_url,
        "board.update_task",
        {
            "task_id": task_id,
            "to_status": "IN_PROGRESS",
            "assigned_to": "writer_1",
        },
    )
    task = _get_task(network, board_url, task_id)
    assert task["status"] == "IN_PROGRESS"
    assert task["assigned_to"] == "writer_1"

    # ── 6. IN_PROGRESS → PENDING_REVIEW (writer posts revised result) ────
    _request(
        network,
        board_url,
        "worker.post_result",
        {
            "task_id": task_id,
            "agent_id": "writer_1",
            "output": "revised draft",
        },
    )
    task = _get_task(network, board_url, task_id)
    assert task["status"] == "PENDING_REVIEW"
    assert task["assigned_to"] == "writer_1"

    # ── 7. PENDING_REVIEW → IN_PROGRESS (reviewer_1 assigned again) ──────
    _request(
        network,
        board_url,
        "board.update_task",
        {
            "task_id": task_id,
            "to_status": "IN_PROGRESS",
            "assigned_to": "reviewer_1",
        },
    )
    task = _get_task(network, board_url, task_id)
    assert task["status"] == "IN_PROGRESS"
    assert task["assigned_to"] == "reviewer_1"

    # ── 8. IN_PROGRESS → APPROVED (reviewer approves) ────────────────────
    _request(
        network,
        board_url,
        "board.update_task",
        {
            "task_id": task_id,
            "to_status": "APPROVED",
            "assigned_to": "reviewer_1",
        },
    )
    task = _get_task(network, board_url, task_id)
    assert task["status"] == "APPROVED"
    assert task["assigned_to"] == "reviewer_1"

    # ── 9. APPROVED → COMPLETE ────────────────────────────────────────────
    _request(
        network,
        board_url,
        "board.update_task",
        {
            "task_id": task_id,
            "to_status": "COMPLETE",
        },
    )
    task = _get_task(network, board_url, task_id)
    assert task["status"] == "COMPLETE"
    assert task["assigned_to"] == "reviewer_1"

    # ── Verify event trail ────────────────────────────────────────────────
    history = _request(
        network, board_url, "board.get_task_history", {"task_id": task_id}
    )
    events = history["events"]

    assigned_events = [e for e in events if e["event_type"] == "task_assigned"]
    assert len(assigned_events) >= 4, (
        f"Expected at least 4 task_assigned events "
        f"(writer, reviewer, writer again, reviewer again); got {len(assigned_events)}"
    )

    reviewed_events = [e for e in events if e["event_type"] == "task_reviewed"]
    assert len(reviewed_events) >= 2, (
        f"Expected at least 2 task_reviewed events "
        f"(rejection + approval + final); got {len(reviewed_events)}"
    )

    to_statuses = [e["to_status"] for e in reviewed_events]
    assert "REVISION_NEEDED" in to_statuses
    assert "APPROVED" in to_statuses

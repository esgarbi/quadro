from __future__ import annotations

import pytest

from quadro import BoardClient, LocalA2ANetwork, QuadroBoard, RunLoop
from quadro.agents.chief import ChiefAgent
from quadro.board.backends.sqlite import SqliteBoardBackend


def _make_client() -> BoardClient:
    network = LocalA2ANetwork()
    board_url = "a2a://board"
    board = QuadroBoard(SqliteBoardBackend(":memory:"))
    network.register_endpoint(board_url, board.handle_request)
    return BoardClient(network, board_url)


def test_board_client_post_and_get_task() -> None:
    client = _make_client()
    task = client.post_task("research", "gut health and anxiety")
    assert task["task_id"]
    assert task["label"] == "gut health and anxiety"
    assert task["status"] == "UNASSIGNED"

    fetched = client.get_task(task["task_id"])
    assert fetched["task_id"] == task["task_id"]
    assert fetched["label"] == "gut health and anxiety"


def test_board_client_raises_on_error() -> None:
    client = _make_client()
    with pytest.raises(RuntimeError):
        client.get_task("non-existent-task-id")


def test_board_client_put_get_data() -> None:
    client = _make_client()
    client.put_data("WH-MAIN", {"SKU-A": 10, "SKU-B": 5})
    value = client.get_data("WH-MAIN")
    assert value == {"SKU-A": 10, "SKU-B": 5}

    # Overwrite
    client.put_data("WH-MAIN", {"SKU-A": 7})
    assert client.get_data("WH-MAIN") == {"SKU-A": 7}

    # Missing key returns None
    assert client.get_data("does-not-exist") is None


def test_board_client_full_state_structure() -> None:
    client = _make_client()
    client.post_task("draft", "article about sleep")
    state = client.full_state()

    assert "tasks" in state
    assert "agents" in state
    assert "data" in state
    assert len(state["tasks"]) == 1
    assert state["tasks"][0]["task_type"] == "draft"


def test_board_vends_client() -> None:
    network = LocalA2ANetwork()
    board = QuadroBoard(
        SqliteBoardBackend(":memory:"), network=network, url="a2a://board"
    )
    bc = board.client()
    assert bc.board_url == "a2a://board"
    assert bc.network is network


def test_board_client_without_network_raises() -> None:
    board = QuadroBoard(SqliteBoardBackend(":memory:"))
    with pytest.raises(RuntimeError, match="network"):
        board.client()


def test_board_url_property() -> None:
    network = LocalA2ANetwork()
    bc = BoardClient(network, "a2a://board")
    assert bc.board_url == "a2a://board"


def test_runloop_accepts_board() -> None:
    network = LocalA2ANetwork()
    board = QuadroBoard(
        SqliteBoardBackend(":memory:"), network=network, url="a2a://board"
    )
    chief = ChiefAgent(network=network, board_url="a2a://board")
    loop = RunLoop(board, chief)
    assert loop._board_client.board_url == "a2a://board"


def test_runloop_still_accepts_board_client() -> None:
    network = LocalA2ANetwork()
    QuadroBoard(SqliteBoardBackend(":memory:"), network=network, url="a2a://board")
    bc = BoardClient(network, "a2a://board")
    chief = ChiefAgent(network=network, board_url="a2a://board")
    loop = RunLoop(bc, chief)
    assert loop._board_client is bc


# ── snapshot() tests ─────────────────────────────────────────────────────────


def _make_snapshot_client(
    custom_profiles: dict | None = None,
) -> BoardClient:
    from quadro.board.state_machine import lifecycle

    network = LocalA2ANetwork()
    board_url = "a2a://board"
    profiles = custom_profiles or {
        "order": lifecycle(
            [
                ("UNASSIGNED", "processing"),
                ("processing", "shipped"),
            ]
        ),
    }
    resolver = {name: name for name in profiles}
    board = QuadroBoard(
        SqliteBoardBackend(":memory:"),
        profile_resolver=resolver,
        custom_profiles=profiles,
    )
    network.register_endpoint(board_url, board.handle_request)
    return BoardClient(network, board_url)


def test_snapshot_returns_none_when_no_active_tasks() -> None:
    client = _make_snapshot_client()
    task = client.post_task("order", "Order #1")
    client.update_task(task["task_id"], "processing")
    client.update_task(task["task_id"], "shipped")
    assert client.snapshot() is None


def test_snapshot_returns_string_when_active_tasks_exist() -> None:
    client = _make_snapshot_client()
    client.post_task("order", "Order #1")
    result = client.snapshot()
    assert isinstance(result, str)
    assert len(result) > 0


def test_snapshot_includes_goal_progress() -> None:
    client = _make_snapshot_client()
    client.put_data("order_goal", {"target_shipped": 10})
    client.post_task("order", "Order #1")
    task2 = client.post_task("order", "Order #2")
    task3 = client.post_task("order", "Order #3")
    client.update_task(task2["task_id"], "processing")
    client.update_task(task2["task_id"], "shipped")
    client.update_task(task3["task_id"], "processing")
    client.update_task(task3["task_id"], "shipped")
    result = client.snapshot(goal_key="order_goal")
    assert result is not None
    assert "Progress: 2/10 shipped" in result


def test_snapshot_groups_tasks_by_status() -> None:
    from quadro.board.state_machine import lifecycle

    client = _make_snapshot_client(
        custom_profiles={
            "order": lifecycle(
                [
                    ("UNASSIGNED", "validating"),
                    ("validating", "checking_stock"),
                    ("checking_stock", "shipped"),
                ]
            ),
        }
    )

    t1 = client.post_task("order", "Order #1")
    t2 = client.post_task("order", "Order #2")
    t3 = client.post_task("order", "Order #3")
    client.update_task(t1["task_id"], "validating")
    client.update_task(t2["task_id"], "validating")
    client.update_task(t3["task_id"], "validating")
    client.update_task(t3["task_id"], "checking_stock")

    result = client.snapshot()
    assert result is not None
    assert "[validating]" in result
    assert "[checking_stock]" in result


def test_snapshot_truncates_long_status_groups() -> None:
    client = _make_client()
    for i in range(6):
        client.post_task("order", f"Order #{i}")
    result = client.snapshot(max_tasks_per_status=5)
    assert result is not None
    assert "\u2026 and 1 more" in result


def test_snapshot_includes_tool_names() -> None:
    from types import SimpleNamespace

    client = _make_client()
    client.post_task("order", "Order #1")
    tools = [SimpleNamespace(name="read_board"), SimpleNamespace(name="advance_order")]
    result = client.snapshot(tools)
    assert result is not None
    assert "read_board" in result
    assert "advance_order" in result


def test_snapshot_custom_terminal_statuses() -> None:
    client = _make_client()
    task = client.post_task("order", "Order #1")
    client.update_task(task["task_id"], "IN_PROGRESS")

    assert client.snapshot() is not None

    assert client.snapshot(terminal_statuses={"IN_PROGRESS"}) is None

from __future__ import annotations

import threading
import time

from quadro import ChiefAgent, LocalA2ANetwork, QuadroBoard, WorkerAgent
from quadro.a2a.contracts import A2ARequest
from quadro.board.backends.sqlite import SqliteBoardBackend


def _make_env(profile: str = "fast") -> tuple[LocalA2ANetwork, str]:
    network = LocalA2ANetwork()
    board_url = "a2a://board"
    board = QuadroBoard(
        SqliteBoardBackend(":memory:"),
        profile_resolver={"work": profile},
    )
    network.register_endpoint(board_url, board.handle_request)
    return network, board_url


def _req(network: LocalA2ANetwork, url: str, intent: str, payload: dict) -> dict:
    resp = network.request(url, A2ARequest(intent=intent, payload=payload).to_dict())
    assert resp["ok"], resp.get("error")
    return resp["result"]


# ── 1. wake() runs a decision cycle ───────────────────────────────────────────


def test_wake_runs_decision_cycle() -> None:
    network, board_url = _make_env()
    chief = ChiefAgent(network=network, board_url=board_url)

    cycles = chief.wake()

    assert cycles == 1
    assert chief.cycles_run == 1


# ── 2. concurrent wake() queues _pending_wake ─────────────────────────────────


def test_concurrent_wake_queues_pending() -> None:
    """
    Two threads call wake() concurrently.  The second must not run a concurrent
    cycle — it sets _pending_wake and the first thread runs a second cycle after
    finishing the first.  Total cycles == 2, never concurrent.
    """
    network, board_url = _make_env()
    concurrent_cycles: list[int] = []

    chief = ChiefAgent(network=network, board_url=board_url)

    original_cycle = chief._run_decision_cycle

    barrier = threading.Barrier(2)
    cycle_lock = threading.Lock()
    active_count = [0]
    max_active = [0]

    def slow_cycle(**kwargs) -> None:
        with cycle_lock:
            active_count[0] += 1
            max_active[0] = max(max_active[0], active_count[0])
        time.sleep(0.02)
        original_cycle(**kwargs)
        with cycle_lock:
            active_count[0] -= 1

    chief._run_decision_cycle = slow_cycle  # type: ignore[method-assign]

    results: list[int] = []

    def call_wake() -> None:
        barrier.wait()
        results.append(chief.wake())

    t1 = threading.Thread(target=call_wake)
    t2 = threading.Thread(target=call_wake)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    assert max_active[0] == 1, "Cycles must never run concurrently"
    assert chief.cycles_run == 2, f"Expected 2 total cycles, got {chief.cycles_run}"
    assert sum(results) == 2  # one thread ran 2 cycles, the other returned 0


# ── 3. nudge() delegates to wake() ────────────────────────────────────────────


def test_nudge_delegates_to_wake() -> None:
    network, board_url = _make_env()
    chief = ChiefAgent(network=network, board_url=board_url)

    nudge_result = chief.nudge()
    wake_cycles_before = chief.cycles_run

    # A second nudge after the first should also return 1
    nudge_result2 = chief.nudge()

    assert nudge_result == 1
    assert nudge_result2 == 1
    assert chief.cycles_run == wake_cycles_before + 1


# ── 4. wake endpoint registered when chief_url provided ───────────────────────


def test_wake_endpoint_registered_when_chief_url_provided() -> None:
    network, board_url = _make_env()
    chief_url = "a2a://chief"
    chief = ChiefAgent(network=network, board_url=board_url, chief_url=chief_url)

    # The endpoint must be reachable — send a valid chief.wake request
    resp = network.request(
        chief_url,
        A2ARequest(intent="chief.wake", payload={}).to_dict(),
    )
    assert resp["ok"]


# ── 5. no endpoint registered without chief_url ───────────────────────────────


def test_wake_endpoint_not_registered_without_chief_url() -> None:
    network, board_url = _make_env()
    ChiefAgent(network=network, board_url=board_url)  # no chief_url

    # The "a2a://chief" URL should not be registered
    import pytest

    with pytest.raises(KeyError, match="No endpoint registered"):
        network.request(
            "a2a://chief",
            A2ARequest(intent="chief.wake", payload={}).to_dict(),
        )


# ── 6. worker wakes chief after completion ────────────────────────────────────


def test_worker_wakes_chief_after_completion() -> None:
    network, board_url = _make_env()
    chief_url = "a2a://chief"

    wake_calls: list[dict] = []

    def mock_chief_handler(envelope: dict) -> dict:
        wake_calls.append(envelope)
        return A2ARequest(intent="chief.wake", payload={}).to_dict() | {
            "ok": True,
            "result": {},
            "error": None,
            "request_id": envelope.get("request_id", "x"),
        }

    network.register_endpoint(chief_url, mock_chief_handler)

    worker = WorkerAgent(
        agent_id="w1",
        name="W1",
        capabilities=["work"],
        url="a2a://w1",
        board_url=board_url,
        network=network,
        execute_fn=lambda ctx, _: "done",
        chief_url=chief_url,
    )
    worker.register()

    task_id = _req(
        network, board_url, "board.post_task", {"task_type": "work", "label": "test"}
    )["task"]["task_id"]
    _req(
        network,
        board_url,
        "board.update_task",
        {"task_id": task_id, "to_status": "IN_PROGRESS"},
    )

    network.request(
        "a2a://w1",
        A2ARequest(
            intent="worker.execute_task", payload={"task_id": task_id}
        ).to_dict(),
    )

    assert len(wake_calls) == 1
    assert wake_calls[0]["intent"] == "chief.wake"


# ── 7. worker wake is fire-and-forget (unreachable chief is swallowed) ─────────


def test_worker_wake_is_fire_and_forget() -> None:
    network, board_url = _make_env()
    # chief_url points to a URL that is NOT registered on the network
    worker = WorkerAgent(
        agent_id="w1",
        name="W1",
        capabilities=["work"],
        url="a2a://w1",
        board_url=board_url,
        network=network,
        execute_fn=lambda ctx, _: "done",
        chief_url="a2a://no-such-chief",
    )
    worker.register()

    task_id = _req(
        network, board_url, "board.post_task", {"task_type": "work", "label": "test"}
    )["task"]["task_id"]
    _req(
        network,
        board_url,
        "board.update_task",
        {"task_id": task_id, "to_status": "IN_PROGRESS"},
    )

    # Must complete without raising, even though chief URL is unreachable
    resp = network.request(
        "a2a://w1",
        A2ARequest(
            intent="worker.execute_task", payload={"task_id": task_id}
        ).to_dict(),
    )
    assert resp["ok"]

    task = _req(network, board_url, "board.get_task", {"task_id": task_id})["task"]
    assert task["status"] == "COMPLETE"

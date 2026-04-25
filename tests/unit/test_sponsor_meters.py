from __future__ import annotations

import time
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

from quadro import ChiefAgent, LocalA2ANetwork, QuadroBoard
from quadro.board.backends.sqlite import SqliteBoardBackend
from quadro.sponsor.meters import (
    BoardEventMeter,
    LlmTokenMeter,
    MeterBundle,
    TickMeter,
    WallClockMeter,
    WorkerInvocationMeter,
)


def test_tick_meter_counts_and_resets() -> None:
    m = TickMeter()
    assert m.value() == 0
    for _ in range(5):
        m.tick()
    assert m.value() == 5
    m.reset()
    assert m.value() == 0


def test_wall_clock_meter_increases() -> None:
    m = WallClockMeter()
    time.sleep(0.01)
    elapsed = m.value()
    assert isinstance(elapsed, timedelta)
    assert elapsed >= timedelta(seconds=0.005)
    m.reset()
    assert m.value() < timedelta(seconds=0.5)


def test_worker_invocation_meter_only_counts_worker_trigger() -> None:
    m = WorkerInvocationMeter()
    m.record("ombudsman")
    m.record("seed")
    m.record("worker")
    m.record("worker")
    assert m.value() == 2


def test_llm_token_meter_accumulates_positive_values() -> None:
    m = LlmTokenMeter()
    m.report(100)
    m.report(0)
    m.report(-5)
    m.report(250)
    assert m.value() == 350


def test_board_event_meter_counts_and_filters() -> None:
    m = BoardEventMeter()
    m.record({"event_type": "task_posted"})
    m.record({"event_type": "task_completed"})
    m.record({"event_type": "task_heartbeat"})
    assert m.value() == 3

    mf = BoardEventMeter(filter=lambda e: e.get("event_type") != "task_heartbeat")
    mf.record({"event_type": "task_posted"})
    mf.record({"event_type": "task_heartbeat"})
    assert mf.value() == 1


def test_meter_bundle_snapshot_and_reset() -> None:
    b = MeterBundle()
    b.ticks.tick()
    b.ticks.tick()
    b.worker_invocations.record("worker")
    b.llm_tokens.report(42)
    b.board_events.record({"event_type": "task_posted"})

    readings = b.snapshot()
    assert readings.ticks == 2
    assert readings.worker_invocations == 1
    assert readings.llm_tokens == 42
    assert readings.board_events == 1
    assert readings.wall_clock_elapsed.total_seconds() >= 0

    b.reset()
    snap = b.snapshot()
    assert snap.ticks == 0 and snap.worker_invocations == 0
    assert snap.llm_tokens == 0 and snap.board_events == 0


def test_bundle_chief_wake_subscriber() -> None:
    b = MeterBundle()
    b.on_chief_wake("worker")
    b.on_chief_wake("ombudsman")
    b.on_chief_wake("worker")
    assert b.snapshot().worker_invocations == 2


def test_bundle_board_event_subscriber_accepts_record_or_dict() -> None:
    b = MeterBundle()
    record = SimpleNamespace(to_dict=lambda: {"event_type": "task_posted"})
    b.on_board_event(record)
    b.on_board_event({"event_type": "task_completed"})
    assert b.snapshot().board_events == 2


# ── Integration: board event observer hook ────────────────────────────────────


def test_board_add_event_listener_fires_on_transitions() -> None:
    board = QuadroBoard(SqliteBoardBackend(), profile_resolver={"work": "fast"})
    seen: list[dict] = []

    def listener(event):
        seen.append(event.to_dict())

    board.add_event_listener(listener)

    from quadro.a2a.contracts import A2ARequest

    board.handle_request(
        A2ARequest(
            intent="board.post_task", payload={"task_type": "work", "label": "x"}
        ).to_dict()
    )
    assert any(e["event_type"] == "task_posted" for e in seen)


def test_board_listener_exceptions_do_not_break_transitions() -> None:
    board = QuadroBoard(SqliteBoardBackend(), profile_resolver={"work": "fast"})

    def bad_listener(event):
        raise RuntimeError("listener exploded")

    board.add_event_listener(bad_listener)

    from quadro.a2a.contracts import A2ARequest

    response = board.handle_request(
        A2ARequest(
            intent="board.post_task", payload={"task_type": "work", "label": "x"}
        ).to_dict()
    )
    assert response["ok"] is True


def test_board_remove_event_listener() -> None:
    board = QuadroBoard(SqliteBoardBackend(), profile_resolver={"work": "fast"})
    seen: list[dict] = []

    def listener(event):
        seen.append(event.to_dict())

    board.add_event_listener(listener)
    board.remove_event_listener(listener)

    from quadro.a2a.contracts import A2ARequest

    board.handle_request(
        A2ARequest(
            intent="board.post_task", payload={"task_type": "work", "label": "x"}
        ).to_dict()
    )
    assert seen == []


# ── Integration: chief wake listener ──────────────────────────────────────────


def test_chief_wake_listener_invoked_for_each_trigger() -> None:
    network = LocalA2ANetwork()
    board = QuadroBoard(
        SqliteBoardBackend(),
        profile_resolver={"work": "fast"},
        network=network,
    )
    bc = board.client()
    chief = ChiefAgent.builder(bc).build()

    triggers: list[str] = []
    chief.add_wake_listener(triggers.append)

    chief.wake(trigger="worker")
    chief.wake(trigger="ombudsman")
    chief.wake(trigger="worker")

    assert triggers == ["worker", "ombudsman", "worker"]


def test_chief_set_draining_updates_state() -> None:
    network = LocalA2ANetwork()
    board = QuadroBoard(
        SqliteBoardBackend(),
        profile_resolver={"work": "fast"},
        network=network,
    )
    bc = board.client()
    chief = ChiefAgent.builder(bc).build()

    assert chief.is_draining() is False
    chief.set_draining(True)
    assert chief.is_draining() is True

    state = bc.full_state()
    assert state["data"].get("_chief_telemetry", {}).get("draining") is True

    chief.set_draining(False)
    assert chief.is_draining() is False


# ── Integration: dispatch_batch honours drain flag ────────────────────────────


def test_dispatch_batch_filters_unassigned_when_draining() -> None:
    from quadro.dispatch import DRAIN_FLAG_KEY, dispatch_batch

    state = {
        "tasks": [
            {"task_id": "t1", "status": "UNASSIGNED", "task_type": "work"},
            {"task_id": "t2", "status": "writing", "task_type": "work"},
        ]
    }

    calls: list[tuple[str, dict]] = []

    def board_fn(intent: str, payload: dict):
        calls.append((intent, payload))
        if intent == "board.get_full_state":
            return state
        if intent == "board.get_data":
            if payload.get("key") == DRAIN_FLAG_KEY:
                return {"key": DRAIN_FLAG_KEY, "value": True}
            return {"key": payload["key"], "value": None}
        if intent == "board.update_task":
            return {"task": {}, "event": {}}
        return {}

    network = MagicMock()

    dispatched, skipped = dispatch_batch(
        board_fn,
        network,
        worker_registry={},
        status_filter={"UNASSIGNED", "writing"},
        target_status="writing",
        capability="writer",
    )

    assert dispatched == [] and skipped == ["t2"]

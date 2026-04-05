from __future__ import annotations

from quadro import BoardClient, ChiefAgent, LocalA2ANetwork, QuadroBoard, WorkerAgent
from quadro.a2a.contracts import A2ARequest
from quadro.board.backends.sqlite import SqliteBoardBackend


def _make_env(profile: str = "fast") -> tuple[LocalA2ANetwork, str, BoardClient]:
    network = LocalA2ANetwork()
    board_url = "a2a://board"
    board = QuadroBoard(
        SqliteBoardBackend(":memory:"),
        profile_resolver={"work": profile},
    )
    network.register_endpoint(board_url, board.handle_request)
    bc = BoardClient(network, board_url)
    return network, board_url, bc


# ── 1. Telemetry written to board after cycle ─────────────────────────────────


def test_chief_telemetry_written_to_board_after_cycle() -> None:
    network, board_url, bc = _make_env()
    chief = ChiefAgent(network=network, board_url=board_url)

    chief.nudge()

    telem = bc.get_data("_chief_telemetry")
    assert telem is not None
    assert "status" in telem
    assert "cycles_run" in telem
    assert "last_woke_at" in telem
    assert "last_slept_at" in telem
    assert "last_cycle_duration_ms" in telem
    assert "last_trigger" in telem
    assert "consecutive_noops" in telem
    assert "recent_durations_ms" in telem


# ── 2. Status is sleeping after nudge completes ───────────────────────────────


def test_chief_telemetry_status_transitions() -> None:
    network, board_url, bc = _make_env()
    chief = ChiefAgent(network=network, board_url=board_url)

    chief.nudge()

    telem = bc.get_data("_chief_telemetry")
    assert telem["status"] == "sleeping"


# ── 3. cycles_run increments ──────────────────────────────────────────────────


def test_chief_telemetry_cycles_run_increments() -> None:
    network, board_url, bc = _make_env()
    chief = ChiefAgent(network=network, board_url=board_url)

    chief.nudge()
    chief.nudge()
    chief.nudge()

    telem = bc.get_data("_chief_telemetry")
    assert telem["cycles_run"] == 3
    assert chief.cycles_run == 3
    assert len(telem["recent_durations_ms"]) == 3


# ── 4. consecutive_noops ──────────────────────────────────────────────────────


def test_chief_telemetry_consecutive_noops() -> None:
    network, board_url, bc = _make_env()

    worker = WorkerAgent(
        agent_id="w1",
        name="W1",
        capabilities=["work"],
        url="a2a://w1",
        board_url=board_url,
        network=network,
        execute_fn=lambda ctx, _: "done",
    )
    worker.register()

    chief = ChiefAgent(network=network, board_url=board_url)

    # No tasks → noop
    chief.nudge()
    telem = bc.get_data("_chief_telemetry")
    assert telem["consecutive_noops"] == 1

    chief.nudge()
    telem = bc.get_data("_chief_telemetry")
    assert telem["consecutive_noops"] == 2

    # Post a task → chief dispatches it → noops reset
    bc.post_task("work", "test task")
    chief.nudge()
    telem = bc.get_data("_chief_telemetry")
    assert telem["consecutive_noops"] == 0


# ── 5. trigger recorded ──────────────────────────────────────────────────────


def test_chief_telemetry_trigger_recorded() -> None:
    network, board_url, bc = _make_env()
    chief_url = "a2a://chief"
    chief = ChiefAgent(
        network=network,
        board_url=board_url,
        chief_url=chief_url,
    )

    chief.nudge(trigger="ombudsman")
    telem = bc.get_data("_chief_telemetry")
    assert telem["last_trigger"] == "ombudsman"

    # Worker wake via A2A endpoint
    network.request(
        chief_url,
        A2ARequest(intent="chief.wake", payload={}).to_dict(),
    )
    telem = bc.get_data("_chief_telemetry")
    assert telem["last_trigger"] == "worker"


# ── 6. Policy mutations are not counted as noops ─────────────────────────────


def test_policy_mutations_not_counted_as_noop() -> None:
    """When the policy mutates the board, the cycle is not a noop — even if
    default routing finds nothing to do afterwards (bug #23)."""
    network, board_url, bc = _make_env()

    worker = WorkerAgent(
        agent_id="w1",
        name="W1",
        capabilities=["work"],
        url="a2a://w1",
        board_url=board_url,
        network=network,
        execute_fn=lambda ctx, _: "done",
    )
    worker.register()

    bc.post_task("work", "task for policy")

    def mutating_policy(ctx: dict) -> None:
        """Policy that dispatches the task itself, leaving nothing for default routing."""
        for task in ctx["payload"]["tasks"]:
            if task["status"] == "UNASSIGNED":
                bc.update_task(task["task_id"], "IN_PROGRESS", assigned_to="w1")

    chief = ChiefAgent(network=network, board_url=board_url, policy=mutating_policy)

    chief.nudge()
    telem = bc.get_data("_chief_telemetry")
    assert (
        telem["consecutive_noops"] == 0
    ), "Policy mutated the board — this cycle should not be a noop"

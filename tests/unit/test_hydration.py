from quadro.agents.hydration import hydrate_chief_context, hydrate_worker_context


def test_hydration_snapshot_hash_stability() -> None:
    state = {
        "tasks": [{"task_id": "1", "status": "UNASSIGNED"}],
        "agents": [{"agent_id": "a", "status": "IDLE"}],
        "data": {},
    }
    event = {"sequence_id": 1, "event_type": "task_posted"}
    one = hydrate_chief_context(state, event)
    two = hydrate_chief_context(state, event)
    assert one["snapshot_hash"] == two["snapshot_hash"]


def test_worker_hydration_is_deterministic() -> None:
    task = {"task_id": "x", "label": "hello"}
    one = hydrate_worker_context(task, notes=["n1"])
    two = hydrate_worker_context(task, notes=["n1"])
    assert one == two

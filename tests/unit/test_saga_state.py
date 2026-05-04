from __future__ import annotations

import json

from quadro.saga.state import SagaState


def test_state_initializes_empty() -> None:
    """A fresh state has no completed steps, evidence, stamps, or fork children."""
    state = SagaState(saga_name="test", pc="first_step")
    assert state.completed_steps == {}
    assert state.evidence == {}
    assert state.stamps == []
    assert state.fork_children == {}
    assert state.waiting_for is None


def test_state_to_board_data_is_json_serializable() -> None:
    """The dict returned by ``to_board_data`` survives a JSON
    round-trip, proving it can be stored via ``board.put_data`` and
    read back via ``board.get_data`` without custom serializers."""
    state = SagaState(
        saga_name="test",
        pc="next",
        idempotency_key="order:42",
        completed_steps={"first": "result_a", "second": {"nested": True}},
    )
    serialized = json.dumps(state.to_board_data())
    deserialized = json.loads(serialized)
    assert deserialized["saga_name"] == "test"
    assert deserialized["pc"] == "next"
    assert deserialized["completed_steps"]["second"]["nested"] is True


def test_state_from_board_data_round_trips() -> None:
    """Storing and reloading produces an equivalent state."""
    original = SagaState(
        saga_name="test",
        pc="next",
        completed_steps={"first": "result"},
        evidence={"intake": {"customer_tier": "gold"}},
    )
    reloaded = SagaState.from_board_data(original.to_board_data())
    assert reloaded is not None
    assert reloaded.saga_name == original.saga_name
    assert reloaded.pc == original.pc
    assert reloaded.completed_steps == original.completed_steps
    assert reloaded.evidence == original.evidence


def test_state_from_none_returns_none() -> None:
    """Reading from a board key that was never written returns ``None``."""
    assert SagaState.from_board_data(None) is None
    assert SagaState.from_board_data({}) is None


def test_state_is_complete_when_pc_is_none() -> None:
    """``is_complete`` is the canonical check for "saga finished"."""
    assert SagaState(saga_name="t", pc=None).is_complete()
    assert not SagaState(saga_name="t", pc="next").is_complete()

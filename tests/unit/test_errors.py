from __future__ import annotations

from quadro import (
    BoardClient,
    ConflictError,
    LocalA2ANetwork,
    NotFoundError,
    QuadroBoard,
    QuadroError,
    TransitionError,
    ValidationError,
)
from quadro.board.backends.sqlite import SqliteBoardBackend


def test_error_hierarchy() -> None:
    assert issubclass(TransitionError, QuadroError)
    assert issubclass(NotFoundError, QuadroError)
    assert issubclass(ConflictError, QuadroError)
    assert issubclass(ValidationError, QuadroError)
    assert issubclass(QuadroError, Exception)


def _make_bc() -> BoardClient:
    network = LocalA2ANetwork()
    board = QuadroBoard(
        SqliteBoardBackend(":memory:"),
        profile_resolver={"work": "fast"},
        network=network,
        url="a2a://board",
    )
    return board.client()


def test_not_found_error_on_missing_task() -> None:
    bc = _make_bc()
    try:
        bc.get_task("nonexistent")
        assert False, "Should have raised"
    except RuntimeError as exc:
        assert "not found" in str(exc).lower()


def test_validation_error_on_bad_agent_card() -> None:
    bc = _make_bc()
    try:
        bc.request("board.register_agent", {"agent_id": "x"})
        assert False, "Should have raised"
    except RuntimeError as exc:
        assert "Missing AgentCard fields" in str(exc)

from __future__ import annotations

import string

from quadro import DefaultTaskIdProvider, QuadroBoard
from quadro.board.backends.sqlite import SqliteBoardBackend
from quadro.board.id_provider import TaskIdProvider

# ── 1. Default provider: length and character set ────────────────────────────


def test_default_provider_generates_5_char_base36_ids() -> None:
    provider = DefaultTaskIdProvider()
    valid_chars = set(string.digits + string.ascii_lowercase)
    for _ in range(50):
        task_id = provider.generate(set())
        assert len(task_id) == 5
        assert all(c in valid_chars for c in task_id)


# ── 2. Default provider: uniqueness within a batch ───────────────────────────


def test_default_provider_generates_unique_ids() -> None:
    provider = DefaultTaskIdProvider()
    ids: set[str] = set()
    for _ in range(200):
        new_id = provider.generate(ids)
        assert new_id not in ids
        ids.add(new_id)


# ── 3. Default provider: collision avoidance ─────────────────────────────────


def test_default_provider_avoids_existing_ids() -> None:
    provider = DefaultTaskIdProvider()
    existing = {"abc12", "xyz99", "00000"}
    for _ in range(50):
        new_id = provider.generate(existing)
        assert new_id not in existing


# ── 4. Default provider: configurable length ─────────────────────────────────


def test_default_provider_respects_custom_length() -> None:
    provider = DefaultTaskIdProvider(length=8)
    task_id = provider.generate(set())
    assert len(task_id) == 8


# ── 5. Default provider: raises on exhaustion ────────────────────────────────


def test_default_provider_raises_on_exhaustion() -> None:
    provider = DefaultTaskIdProvider(length=1)
    all_single_chars = set(string.digits + string.ascii_lowercase)
    try:
        provider.generate(all_single_chars)
        assert False, "Should have raised RuntimeError"
    except RuntimeError as exc:
        assert "100 attempts" in str(exc)


# ── 6. Custom provider: board accepts custom provider ────────────────────────


class SequentialProvider:
    """Test provider that produces predictable sequential IDs."""

    def __init__(self, prefix: str = "T") -> None:
        self._counter = 0
        self._prefix = prefix

    def generate(self, existing_ids: set[str]) -> str:
        self._counter += 1
        return f"{self._prefix}{self._counter:04d}"


def test_custom_provider_satisfies_protocol() -> None:
    provider = SequentialProvider()
    assert isinstance(provider, TaskIdProvider)


def test_board_uses_custom_provider() -> None:
    provider = SequentialProvider(prefix="ORD-")
    board = QuadroBoard(
        SqliteBoardBackend(":memory:"),
        profile_resolver={"work": "fast"},
        id_provider=provider,
    )

    from quadro import BoardClient, LocalA2ANetwork

    network = LocalA2ANetwork()
    network.register_endpoint("a2a://board", board.handle_request)
    bc = BoardClient(network, "a2a://board")

    t1 = bc.post_task("work", "first task")
    t2 = bc.post_task("work", "second task")
    t3 = bc.post_task("work", "third task")

    assert t1["task_id"] == "ORD-0001"
    assert t2["task_id"] == "ORD-0002"
    assert t3["task_id"] == "ORD-0003"


# ── 7. Caller-supplied task_id bypasses the provider ─────────────────────────


def test_caller_supplied_id_bypasses_provider() -> None:
    provider = SequentialProvider()
    board = QuadroBoard(
        SqliteBoardBackend(":memory:"),
        profile_resolver={"work": "fast"},
        id_provider=provider,
    )

    from quadro import BoardClient, LocalA2ANetwork

    network = LocalA2ANetwork()
    network.register_endpoint("a2a://board", board.handle_request)
    bc = BoardClient(network, "a2a://board")

    task = bc.post_task("work", "explicit id", task_id="MY-ID")
    assert task["task_id"] == "MY-ID"

    next_task = bc.post_task("work", "auto id")
    assert next_task["task_id"] == "T0001"


# ── 8. Board without explicit provider uses default ──────────────────────────


def test_board_default_provider_generates_short_ids() -> None:
    board = QuadroBoard(
        SqliteBoardBackend(":memory:"),
        profile_resolver={"work": "fast"},
    )

    from quadro import BoardClient, LocalA2ANetwork

    network = LocalA2ANetwork()
    network.register_endpoint("a2a://board", board.handle_request)
    bc = BoardClient(network, "a2a://board")

    task = bc.post_task("work", "auto id")
    assert len(task["task_id"]) == 5
    valid_chars = set(string.digits + string.ascii_lowercase)
    assert all(c in valid_chars for c in task["task_id"])

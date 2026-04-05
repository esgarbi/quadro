from __future__ import annotations

from quadro import BoardClient, LocalA2ANetwork, QuadroBoard, WorkerPool
from quadro.board.backends.sqlite import SqliteBoardBackend


def _make_env() -> tuple[LocalA2ANetwork, str, BoardClient]:
    network = LocalA2ANetwork()
    board_url = "a2a://board"
    board = QuadroBoard(
        SqliteBoardBackend(":memory:"),
        profile_resolver={"alpha": "fast", "beta": "fast", "gamma": "fast"},
    )
    network.register_endpoint(board_url, board.handle_request)
    bc = BoardClient(network, board_url)
    return network, board_url, bc


def _noop(ctx: dict, board_fn) -> str:
    return "ok"


# ── 1. Pool registers correct agent count ─────────────────────────────────────


def test_pool_registers_correct_agent_count() -> None:
    _, _, bc = _make_env()

    pool = (
        WorkerPool(bc)
        .workers(2)
        .add("alpha", _noop)
        .add("beta", _noop)
        .add("gamma", _noop)
        .build()
    )

    assert len(pool.agents) == 6
    state = bc.full_state()
    assert len(state["agents"]) == 6


# ── 2. Registry structure ─────────────────────────────────────────────────────


def test_pool_registry_structure() -> None:
    _, _, bc = _make_env()

    pool = WorkerPool(bc).workers(2).add("alpha", _noop).add("beta", _noop).build()

    reg = pool.registry
    assert set(reg.keys()) == {"alpha", "beta"}
    for cap in ("alpha", "beta"):
        entries = reg[cap]
        assert len(entries) == 2
        for agent_id, url in entries:
            assert cap in agent_id
            assert url.startswith("a2a://workers/")


# ── 3. Working statuses populated ─────────────────────────────────────────────


def test_pool_working_statuses_populated() -> None:
    _, _, bc = _make_env()

    pool = (
        WorkerPool(bc)
        .add("alpha", _noop, active_status="doing_alpha")
        .add("beta", _noop, active_status="doing_beta")
        .build()
    )

    assert pool.working_statuses == frozenset({"doing_alpha", "doing_beta"})


# ── 4. Working statuses empty when not specified ──────────────────────────────


def test_pool_working_statuses_empty_when_not_specified() -> None:
    _, _, bc = _make_env()

    pool = WorkerPool(bc).add("alpha", _noop).build()

    assert pool.working_statuses == frozenset()


# ── 5. Build is idempotent ────────────────────────────────────────────────────


def test_pool_build_idempotent() -> None:
    _, _, bc = _make_env()

    pool = WorkerPool(bc).workers(2).add("alpha", _noop).build()

    count_after_first = len(pool.agents)
    state_after_first = bc.full_state()
    agent_count_first = len(state_after_first["agents"])

    pool.build()

    assert len(pool.agents) == count_after_first
    state_after_second = bc.full_state()
    assert len(state_after_second["agents"]) == agent_count_first


# ── 6. Status timeouts populated ─────────────────────────────────────────────


def test_pool_status_timeouts_populated() -> None:
    _, _, bc = _make_env()

    pool = (
        WorkerPool(bc)
        .add("validation", _noop, active_status="validating", max_working_time=2.0)
        .build()
    )

    assert pool.status_timeouts == {"validating": 120}


# ── 7. Default timeout applied when active_status set but no explicit timeout ─


def test_pool_status_timeouts_default_when_not_specified() -> None:
    _, _, bc = _make_env()

    pool = WorkerPool(bc).add("research", _noop, active_status="researching").build()

    assert pool.status_timeouts == {"researching": 1800}


# ── 8. Status timeout ignored without active_status ──────────────────────────


def test_pool_status_timeout_ignored_without_active_status() -> None:
    _, _, bc = _make_env()

    pool = WorkerPool(bc).add("review", _noop, max_working_time=1.0).build()

    assert pool.status_timeouts == {}


# ── 9. max_working_time converts to seconds ──────────────────────────────────


def test_max_working_time_converts_to_seconds() -> None:
    _, _, bc = _make_env()

    pool = (
        WorkerPool(bc)
        .add("validation", _noop, active_status="validating", max_working_time=2.0)
        .build()
    )

    assert pool.status_timeouts["validating"] == 120


# ── 10. max_working_time fractional ──────────────────────────────────────────


def test_max_working_time_fractional() -> None:
    _, _, bc = _make_env()

    pool = (
        WorkerPool(bc)
        .add("inventory", _noop, active_status="checking_stock", max_working_time=0.5)
        .build()
    )

    assert pool.status_timeouts["checking_stock"] == 30


# ── 11. Default applied when no timeout given ────────────────────────────────


def test_default_applied_when_no_timeout_given() -> None:
    _, _, bc = _make_env()

    pool = WorkerPool(bc).add("writing", _noop, active_status="writing").build()

    assert pool.status_timeouts["writing"] == int(30 * 60)


# ── 12. stale_timeout_seconds still works with warning ───────────────────────


def test_stale_timeout_seconds_still_works_with_warning() -> None:
    import warnings as _warnings

    _, _, bc = _make_env()

    with _warnings.catch_warnings(record=True) as w:
        _warnings.simplefilter("always")
        pool = (
            WorkerPool(bc)
            .add("cap", _noop, active_status="s", stale_timeout_seconds=60)
            .build()
        )
        deprecation_warnings = [
            x for x in w if issubclass(x.category, DeprecationWarning)
        ]
        assert any(
            "stale_timeout_seconds" in str(x.message) for x in deprecation_warnings
        )

    assert pool.status_timeouts["s"] == 60


# ── 13. pool.ombudsman() returns configured Ombudsman ─────────────────────────


def test_pool_ombudsman_returns_configured_ombudsman() -> None:
    from quadro.ombudsman import Ombudsman

    _, _, bc = _make_env()

    pool = (
        WorkerPool(bc)
        .workers(1)
        .wakes("a2a://chief")
        .add("v", _noop, active_status="validating", max_working_time=2.0)
        .build()
    )
    wd = pool.ombudsman()

    assert isinstance(wd, Ombudsman)


# ── 14. pool.ombudsman() before build raises ──────────────────────────────────


def test_pool_ombudsman_before_build_raises() -> None:
    import pytest

    _, _, bc = _make_env()

    pool = WorkerPool(bc).add("v", _noop)

    with pytest.raises(RuntimeError, match="after .build"):
        pool.ombudsman()


# ── 15. pool.ombudsman() custom default ───────────────────────────────────────


def test_pool_ombudsman_custom_default() -> None:
    from quadro.ombudsman import Ombudsman

    _, _, bc = _make_env()

    pool = (
        WorkerPool(bc)
        .workers(1)
        .wakes("a2a://chief")
        .add("v", _noop, active_status="validating", max_working_time=2.0)
        .build()
    )
    wd = pool.ombudsman(default_timeout_minutes=5.0)

    assert isinstance(wd, Ombudsman)
    assert wd.heartbeat_timeout_seconds == 300

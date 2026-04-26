"""Coverage for the async internals of :class:`RunLoop`.

``RunLoop.run()`` is kept synchronous on the outside (see
``tests/unit/test_run_loop.py`` for the sync-facing contract); this file
pins the async primitives introduced in Phase 2:

* ``RunLoop.run_async()`` works when the caller is already inside an event
  loop.
* A chief wake preempts the ``poll_interval`` sleep, so workers can drive
  the loop without waiting out a clock.
* Sponsors continue to run on their synchronous Protocol, unchanged.
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from quadro import BoardClient, ChiefAgent, LocalA2ANetwork, QuadroBoard, RunLoop
from quadro.board.backends.sqlite import SqliteBoardBackend
from quadro.sponsor import (
    AlwaysStopSponsor,
    Continue,
    GoalSponsor,
    Lease,
    ScriptedSponsor,
    Stop,
    TickBudgetSponsor,
)


def _make_env() -> tuple[LocalA2ANetwork, str, BoardClient, ChiefAgent, QuadroBoard]:
    network = LocalA2ANetwork()
    board = QuadroBoard(
        SqliteBoardBackend(":memory:"),
        profile_resolver={"work": "fast"},
        network=network,
    )
    bc = board.client()
    chief = ChiefAgent.builder(bc).build()
    return network, board._url, bc, chief, board


# ── run_async() works from an existing event loop ───────────────────────────


def test_run_async_returns_from_existing_loop() -> None:
    _, _, _, chief, board = _make_env()

    async def _driver() -> dict:
        return await (
            RunLoop(board, chief)
            .sponsor(AlwaysStopSponsor())
            .poll_every(0.0)
            .run_async()
        )

    state = asyncio.run(_driver())
    assert isinstance(state, dict)
    assert "tasks" in state


def test_run_async_raises_without_sponsor() -> None:
    _, _, bc, chief, _ = _make_env()

    async def _driver() -> None:
        await RunLoop(bc, chief).run_async()

    with pytest.raises(ValueError, match="sponsor"):
        asyncio.run(_driver())


# ── Chief wake preempts the poll_interval wait ───────────────────────────────


def test_chief_wake_preempts_poll_interval() -> None:
    """A chief wake during the wait should bump the loop forward sooner than
    the configured ``poll_every``.

    The test runs with a 2-second poll interval but fires a wake ~50ms into
    the first wait. The run must complete well before the nominal
    2 * poll_interval ceiling would allow, proving the event preempted the
    timeout.
    """
    _, _, _, chief, board = _make_env()

    # Two Continues then Stop: requires two ticks to complete. With
    # poll_every=2.0 the run would take ~4s without the event-driven wakeup.
    script = [
        Continue(lease=Lease(ticks=1)),
        Continue(lease=Lease(ticks=1)),
        Stop(reason="done"),
    ]

    def _wake_soon() -> None:
        time.sleep(0.05)
        chief.wake(trigger="worker")

    waker = threading.Thread(target=_wake_soon, daemon=True)
    waker.start()

    t0 = time.monotonic()
    (RunLoop(board, chief).sponsor(ScriptedSponsor(script)).poll_every(2.0).run())
    elapsed = time.monotonic() - t0

    waker.join(timeout=1.0)

    # Without event-driven wake, the first tick alone would cost 2.0s. A
    # comfortable ceiling of 1.0s proves the preemption fired.
    assert elapsed < 1.0, f"run took {elapsed:.3f}s; expected event-driven preempt"


# ── Sponsor Protocol stays sync — a synchronous sponsor still works ──────────


def test_run_async_calls_sync_sponsor_via_thread() -> None:
    """The sponsor's propose_lease is called from a thread, not the loop."""
    _, _, _, chief, board = _make_env()

    seen_thread_ids: list[int] = []

    class _ThreadProbeSponsor:
        name = "thread_probe"
        fail_open = False

        def propose_lease(self, ctx, prior):  # noqa: D401
            seen_thread_ids.append(threading.get_ident())
            if len(seen_thread_ids) >= 2:
                return Stop(reason="done")
            return Continue(lease=Lease(ticks=1))

    (RunLoop(board, chief).sponsor(_ThreadProbeSponsor()).poll_every(0.0).run())

    # Each consult should execute on a worker thread, not on the asyncio loop
    # thread. asyncio.to_thread uses the default executor, which in the
    # single-run case tends to use the same background thread across calls.
    assert len(seen_thread_ids) >= 2
    main_thread_id = threading.main_thread().ident
    assert all(t != main_thread_id for t in seen_thread_ids), (
        "Sponsor ran on the main thread; asyncio.to_thread bridging expected"
    )


# ── Goal met still terminates cleanly ────────────────────────────────────────


def test_run_async_exits_on_goal_met() -> None:
    _, _, _, chief, board = _make_env()

    async def _driver() -> dict:
        return await (
            RunLoop(board, chief)
            .sponsor(GoalSponsor(lambda _s: True))
            .poll_every(0.0)
            .run_async()
        )

    state = asyncio.run(_driver())
    assert isinstance(state, dict)


# ── Tick budget bounds the run even without wakes ────────────────────────────


def test_run_async_respects_tick_budget() -> None:
    _, _, _, chief, board = _make_env()

    (RunLoop(board, chief).sponsor(TickBudgetSponsor(3)).poll_every(0.0).run())

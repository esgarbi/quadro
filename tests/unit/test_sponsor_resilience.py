"""P9 — error handling and resilience tests.

Ensures that Sponsor failures never crash the RunLoop, that invalid leases
are clamped rather than propagated, and that fail_open behaves as specified.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


from quadro import (
    ChiefAgent,
    LocalA2ANetwork,
    QuadroBoard,
    RunLoop,
)
from quadro.board.backends.sqlite import SqliteBoardBackend
from quadro.sponsor import (
    AllOf,
    Continue,
    DeadlineSponsor,
    Lease,
    SponsorContext,
    Stop,
    TickBudgetSponsor,
)


def _make_env():
    network = LocalA2ANetwork()
    board = QuadroBoard(
        SqliteBoardBackend(":memory:"),
        profile_resolver={"work": "fast"},
        network=network,
    )
    bc = board.client()
    chief = ChiefAgent.builder(bc).build()
    return bc, chief, board


# ── Invalid lease clamping ────────────────────────────────────────────────────


def test_invalid_lease_is_clamped_before_use() -> None:
    past = datetime.now(timezone.utc) - timedelta(days=1)

    class _Bad:
        name = "bad"
        fail_open = False

        def propose_lease(self, ctx, prior):
            return Continue(
                lease=Lease(ticks=-5, deadline=past, llm_tokens=-100),
                reason="oops",
            )

    bc, chief, board = _make_env()
    (
        RunLoop(board, chief)
        .sponsor(AllOf(_Bad(), TickBudgetSponsor(3)))
        .poll_every(0.0)
        .run()
    )
    # Loop exits cleanly without raising — the test passes if we reach here.


# ── Raising Sponsor fails closed by default ───────────────────────────────────


def test_raising_sponsor_fails_closed() -> None:
    class _Exploder:
        name = "exploder"
        fail_open = False

        def propose_lease(self, ctx, prior):
            raise RuntimeError("kaboom")

    bc, chief, board = _make_env()
    final = RunLoop(board, chief).sponsor(_Exploder()).poll_every(0.0).run()
    log = bc.full_state()["data"].get("_sponsor_log") or []
    assert any(
        entry["decision"] == "stop" and "sponsor_error" in entry["reason"]
        for entry in log
    )
    assert isinstance(final, dict)


# ── Raising Sponsor with fail_open renews the prior lease ─────────────────────


def test_raising_sponsor_with_fail_open_renews_prior() -> None:
    calls = {"n": 0}

    class _FlakyThenBoom:
        name = "flaky"
        fail_open = True

        def propose_lease(self, ctx, prior):
            calls["n"] += 1
            if calls["n"] == 1:
                return Continue(lease=Lease(ticks=1), reason="ok")
            if calls["n"] < 4:
                raise RuntimeError(f"transient_{calls['n']}")
            # Eventually Stop so the loop terminates.
            return Stop(reason="done")

    bc, chief, board = _make_env()
    (RunLoop(board, chief).sponsor(_FlakyThenBoom()).poll_every(0.0).run())
    log = bc.full_state()["data"].get("_sponsor_log") or []
    # We should see at least one Continue (the fail-open renewal) between the
    # initial Continue and the final Stop.
    continues = [e for e in log if e["decision"] == "continue"]
    assert len(continues) >= 2


# ── Lease.clamp() preserves valid leases unchanged ────────────────────────────


def test_lease_clamp_identity_for_valid_lease() -> None:
    future = datetime.now(timezone.utc) + timedelta(minutes=5)
    lease = Lease(ticks=10, deadline=future, llm_tokens=1000)
    assert lease.clamp() is lease


# ── Deadlines with naive datetimes are auto-utc-ified ─────────────────────────


def test_deadline_sponsor_accepts_naive_datetime_as_utc() -> None:
    naive = datetime(2030, 1, 1, 12, 0, 0)
    sponsor = DeadlineSponsor(naive)
    # Not raising is the success condition; propose_lease should treat it as UTC.
    ctx = SponsorContext(
        state={"tasks": [], "agents": [], "data": {}},
        chief_telemetry={},
        meters=__import__("quadro.sponsor", fromlist=["MeterReadings"]).MeterReadings(),
        lease_history=(),
        now=datetime(2029, 12, 1, tzinfo=timezone.utc),
    )
    d = sponsor.propose_lease(ctx, prior=None)
    assert isinstance(d, Continue)
    assert d.lease.deadline is not None


# ── HTTP sponsor error paths covered separately (see test_sponsor_external) ──

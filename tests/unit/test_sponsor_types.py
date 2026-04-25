from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from quadro.sponsor import (
    AlwaysOnSponsor,
    AlwaysStopSponsor,
    Continue,
    Drain,
    Lease,
    MeterReadings,
    ScriptedSponsor,
    Sponsor,
    SponsorContext,
    Stop,
    new_lease_id,
)


def _ctx(now: datetime | None = None) -> SponsorContext:
    return SponsorContext(
        state={"tasks": [], "agents": [], "data": {}},
        chief_telemetry={},
        meters=MeterReadings(),
        lease_history=(),
        now=now or datetime.now(timezone.utc),
    )


def test_new_lease_id_is_unique() -> None:
    ids = {new_lease_id() for _ in range(1000)}
    assert len(ids) == 1000


def test_lease_defaults_are_unbounded() -> None:
    lease = Lease()
    assert lease.ticks is None
    assert lease.deadline is None
    assert lease.worker_invocations is None
    assert lease.llm_tokens is None
    assert lease.board_events is None
    assert lease.source == "anonymous"
    assert lease.renewal_of is None


def test_lease_clamp_fixes_negative_budgets_and_past_deadlines() -> None:
    past = datetime.now(timezone.utc) - timedelta(days=1)
    lease = Lease(
        ticks=-3,
        deadline=past,
        worker_invocations=-1,
        llm_tokens=-50,
        board_events=-9,
    )
    clamped = lease.clamp()
    assert clamped.ticks == 0
    assert clamped.worker_invocations == 0
    assert clamped.llm_tokens == 0
    assert clamped.board_events == 0
    assert clamped.deadline is not None
    assert clamped.deadline >= datetime.now(timezone.utc) - timedelta(seconds=1)


def test_lease_clamp_preserves_valid_values() -> None:
    future = datetime.now(timezone.utc) + timedelta(minutes=5)
    lease = Lease(ticks=5, deadline=future, llm_tokens=1000)
    assert lease.clamp() is lease  # no changes -> identity


def test_lease_to_dict_roundtrippable_shape() -> None:
    future = datetime(2030, 1, 1, tzinfo=timezone.utc)
    lease = Lease(
        id="abc123",
        deadline=future,
        ticks=3,
        source="test",
        reason="exploration",
        renewal_of="prev01",
    )
    d = lease.to_dict()
    assert d["id"] == "abc123"
    assert d["ticks"] == 3
    assert d["deadline"] == future.isoformat()
    assert d["source"] == "test"
    assert d["renewal_of"] == "prev01"


def test_meter_readings_to_dict_shape() -> None:
    readings = MeterReadings(
        ticks=10,
        wall_clock_elapsed=timedelta(seconds=42.5),
        worker_invocations=4,
        llm_tokens=1234,
        board_events=9,
    )
    d = readings.to_dict()
    assert d["ticks"] == 10
    assert d["wall_clock_elapsed_s"] == pytest.approx(42.5)
    assert d["worker_invocations"] == 4
    assert d["llm_tokens"] == 1234
    assert d["board_events"] == 9


# ── Sponsor Protocol conformance ──────────────────────────────────────────────


def test_always_on_is_sponsor() -> None:
    assert isinstance(AlwaysOnSponsor(), Sponsor)


def test_always_stop_is_sponsor() -> None:
    assert isinstance(AlwaysStopSponsor(), Sponsor)


def test_scripted_is_sponsor() -> None:
    assert isinstance(ScriptedSponsor(script=[]), Sponsor)


# ── AlwaysOnSponsor ───────────────────────────────────────────────────────────


def test_always_on_returns_continue_with_unbounded_lease_by_default() -> None:
    sponsor = AlwaysOnSponsor()
    decision = sponsor.propose_lease(_ctx(), prior=None)
    assert isinstance(decision, Continue)
    assert decision.lease.ticks is None
    assert decision.lease.deadline is None
    assert decision.lease.source == "always_on"


def test_always_on_respects_kwargs_and_records_renewal() -> None:
    sponsor = AlwaysOnSponsor(ticks=3, deadline_seconds=60)
    prior = Lease(id="prev01", source="x")
    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    ctx = _ctx(now=now)
    decision = sponsor.propose_lease(ctx, prior=prior)
    assert isinstance(decision, Continue)
    assert decision.lease.ticks == 3
    assert decision.lease.deadline == now + timedelta(seconds=60)
    assert decision.lease.renewal_of == "prev01"


# ── AlwaysStopSponsor ─────────────────────────────────────────────────────────


def test_always_stop_returns_stop() -> None:
    sponsor = AlwaysStopSponsor(reason="testing_stop")
    decision = sponsor.propose_lease(_ctx(), prior=None)
    assert isinstance(decision, Stop)
    assert decision.reason == "testing_stop"


# ── ScriptedSponsor ───────────────────────────────────────────────────────────


def test_scripted_walks_the_script_and_defaults_to_stop() -> None:
    script = [
        Continue(lease=Lease(ticks=2), reason="step1"),
        Drain(deadline=None, reason="step2"),
    ]
    sponsor = ScriptedSponsor(script)
    ctx = _ctx()

    d1 = sponsor.propose_lease(ctx, prior=None)
    assert isinstance(d1, Continue)
    assert d1.lease.ticks == 2
    assert d1.reason == "step1"

    d2 = sponsor.propose_lease(ctx, prior=d1.lease)
    assert isinstance(d2, Drain)
    assert d2.reason == "step2"

    d3 = sponsor.propose_lease(ctx, prior=d1.lease)
    assert isinstance(d3, Stop)
    assert d3.reason == "script_exhausted"

    assert sponsor.calls == 3


def test_scripted_custom_default() -> None:
    sponsor = ScriptedSponsor(script=[], default=Drain(deadline=None, reason="end"))
    d = sponsor.propose_lease(_ctx(), prior=None)
    assert isinstance(d, Drain)
    assert d.reason == "end"


def test_scripted_patches_issued_at_and_renewal() -> None:
    script = [Continue(lease=Lease(ticks=5))]
    sponsor = ScriptedSponsor(script)
    prior = Lease(id="prev01")
    now = datetime(2030, 6, 1, tzinfo=timezone.utc)
    ctx = _ctx(now=now)
    d = sponsor.propose_lease(ctx, prior=prior)
    assert isinstance(d, Continue)
    assert d.lease.issued_at == now
    assert d.lease.renewal_of == "prev01"
    assert d.lease.ticks == 5

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from quadro.sponsor import (
    AllOf,
    AlwaysOnSponsor,
    AlwaysStopSponsor,
    AnyOf,
    Continue,
    DeadlineSponsor,
    Drain,
    GoalSponsor,
    Lease,
    MeterReadings,
    Priority,
    ScriptedSponsor,
    SponsorContext,
    Stop,
    TickBudgetSponsor,
)


def _ctx(
    *,
    now: datetime | None = None,
    meters: MeterReadings | None = None,
    history: tuple[Lease, ...] = (),
) -> SponsorContext:
    return SponsorContext(
        state={"tasks": [], "agents": [], "data": {}},
        chief_telemetry={},
        meters=meters or MeterReadings(),
        lease_history=history,
        now=now or datetime.now(timezone.utc),
    )


# ── AllOf ─────────────────────────────────────────────────────────────────────


def test_all_of_requires_at_least_one_child() -> None:
    with pytest.raises(ValueError):
        AllOf()


def test_all_of_intersects_lease_axes() -> None:
    a = AlwaysOnSponsor(name="a", ticks=5)
    b = AlwaysOnSponsor(name="b", ticks=10)
    sponsor = AllOf(a, b)
    d = sponsor.propose_lease(_ctx(), prior=None)
    assert isinstance(d, Continue)
    assert d.lease.ticks == 5
    assert d.lease.source == "all_of"


def test_all_of_short_circuits_on_stop() -> None:
    sponsor = AllOf(AlwaysOnSponsor(), AlwaysStopSponsor(reason="child"))
    d = sponsor.propose_lease(_ctx(), prior=None)
    assert isinstance(d, Stop)
    assert "child" in d.reason
    assert d.reason.startswith("all_of:")


def test_all_of_drain_plus_continue_yields_drain() -> None:
    drain_child = ScriptedSponsor(
        [Drain(deadline=None, reason="child_drain")],
        default=Drain(deadline=None, reason="child_drain"),
    )
    sponsor = AllOf(AlwaysOnSponsor(), drain_child)
    d = sponsor.propose_lease(_ctx(), prior=None)
    assert isinstance(d, Drain)
    assert "child_drain" in d.reason


def test_all_of_drain_deadline_is_min_of_children() -> None:
    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    d1 = Drain(deadline=now + timedelta(minutes=2), reason="early")
    d2 = Drain(deadline=now + timedelta(minutes=10), reason="late")
    sponsor = AllOf(
        ScriptedSponsor([d1], default=d1),
        ScriptedSponsor([d2], default=d2),
    )
    d = sponsor.propose_lease(_ctx(now=now), prior=None)
    assert isinstance(d, Drain)
    assert d.deadline == now + timedelta(minutes=2)


# ── AnyOf ─────────────────────────────────────────────────────────────────────


def test_any_of_continue_dominates_stop() -> None:
    sponsor = AnyOf(AlwaysStopSponsor(reason="nope"), AlwaysOnSponsor())
    d = sponsor.propose_lease(_ctx(), prior=None)
    assert isinstance(d, Continue)


def test_any_of_continue_dominates_drain() -> None:
    drain_child = ScriptedSponsor(
        [Drain(deadline=None, reason="d")], default=Drain(deadline=None, reason="d")
    )
    sponsor = AnyOf(AlwaysOnSponsor(), drain_child)
    d = sponsor.propose_lease(_ctx(), prior=None)
    assert isinstance(d, Continue)


def test_any_of_drain_dominates_stop() -> None:
    drain_child = ScriptedSponsor(
        [Drain(deadline=None, reason="d")], default=Drain(deadline=None, reason="d")
    )
    sponsor = AnyOf(AlwaysStopSponsor(reason="s"), drain_child)
    d = sponsor.propose_lease(_ctx(), prior=None)
    assert isinstance(d, Drain)


def test_any_of_all_stop_stops() -> None:
    sponsor = AnyOf(AlwaysStopSponsor(reason="a"), AlwaysStopSponsor(reason="b"))
    d = sponsor.propose_lease(_ctx(), prior=None)
    assert isinstance(d, Stop)
    assert d.reason.startswith("any_of:")


def test_any_of_union_lease_axes() -> None:
    a = AlwaysOnSponsor(name="a", ticks=5)
    b = AlwaysOnSponsor(name="b", ticks=20)
    sponsor = AnyOf(a, b)
    d = sponsor.propose_lease(_ctx(), prior=None)
    assert isinstance(d, Continue)
    assert d.lease.ticks == 20


def test_any_of_drain_deadline_is_max_of_children() -> None:
    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    d1 = Drain(deadline=now + timedelta(minutes=2), reason="a")
    d2 = Drain(deadline=now + timedelta(minutes=10), reason="b")
    sponsor = AnyOf(
        ScriptedSponsor([d1], default=d1),
        ScriptedSponsor([d2], default=d2),
    )
    d = sponsor.propose_lease(_ctx(now=now), prior=None)
    assert isinstance(d, Drain)
    assert d.deadline == now + timedelta(minutes=10)


# ── Priority ──────────────────────────────────────────────────────────────────


def test_priority_first_continue_wins_and_shortcuts() -> None:
    probe = []

    class _Recorder:
        name = "rec"
        fail_open = False

        def propose_lease(self, ctx, prior):
            probe.append("called")
            return Continue(lease=Lease(ticks=7))

    sponsor = Priority(AlwaysOnSponsor(name="first"), _Recorder())
    d = sponsor.propose_lease(_ctx(), prior=None)
    assert isinstance(d, Continue)
    assert probe == []  # second not consulted after first Continue


def test_priority_drain_also_shortcuts() -> None:
    probe = []

    class _Recorder:
        name = "rec"
        fail_open = False

        def propose_lease(self, ctx, prior):
            probe.append("called")
            return Continue(lease=Lease())

    drain_child = ScriptedSponsor(
        [Drain(deadline=None, reason="d")], default=Drain(deadline=None, reason="d")
    )
    sponsor = Priority(drain_child, _Recorder())
    d = sponsor.propose_lease(_ctx(), prior=None)
    assert isinstance(d, Drain)
    assert probe == []


def test_priority_falls_through_stops() -> None:
    sponsor = Priority(
        AlwaysStopSponsor(reason="first_stop"),
        AlwaysOnSponsor(name="fallback"),
    )
    d = sponsor.propose_lease(_ctx(), prior=None)
    assert isinstance(d, Continue)
    assert d.lease.source == "priority"


def test_priority_all_stop_returns_stop() -> None:
    sponsor = Priority(AlwaysStopSponsor(reason="a"), AlwaysStopSponsor(reason="b"))
    d = sponsor.propose_lease(_ctx(), prior=None)
    assert isinstance(d, Stop)


# ── Child error handling ──────────────────────────────────────────────────────


def test_composer_absorbs_child_exception_as_stop_unless_fail_open() -> None:
    class _Explode:
        name = "boom"
        fail_open = False

        def propose_lease(self, ctx, prior):
            raise RuntimeError("kaboom")

    sponsor = AllOf(_Explode(), AlwaysOnSponsor())
    d = sponsor.propose_lease(_ctx(), prior=None)
    assert isinstance(d, Stop)
    assert "child_sponsor_error" in d.reason


def test_composer_child_fail_open_renews_prior() -> None:
    class _Explode:
        name = "boom"
        fail_open = True

        def propose_lease(self, ctx, prior):
            raise RuntimeError("kaboom")

    sponsor = AllOf(_Explode(), AlwaysOnSponsor())
    prior = Lease(id="prev99", ticks=4)
    d = sponsor.propose_lease(_ctx(), prior=prior)
    # AllOf with a renewed prior + AlwaysOn (unbounded) => min = 4
    assert isinstance(d, Continue)
    assert d.lease.ticks == 4


# ── Nested composition ────────────────────────────────────────────────────────


def test_nested_composition_round_trips() -> None:
    inner = AnyOf(AlwaysStopSponsor(reason="x"), AlwaysOnSponsor(name="inner"))
    outer = AllOf(inner, TickBudgetSponsor(10))
    d = outer.propose_lease(_ctx(), prior=None)
    assert isinstance(d, Continue)
    # TickBudget absolute ceiling = 10, AnyOf inner = unbounded → min = 10
    assert d.lease.ticks == 10


def test_goal_and_deadline_composite_is_canonical() -> None:
    sponsor = AllOf(
        GoalSponsor(lambda s: False),
        DeadlineSponsor.from_now(minutes=5),
        TickBudgetSponsor(100),
    )
    d = sponsor.propose_lease(_ctx(), prior=None)
    assert isinstance(d, Continue)
    assert d.lease.deadline is not None
    assert d.lease.ticks is not None


# ── Associativity sanity (property-ish) ───────────────────────────────────────


def test_all_of_is_associative_for_bounded_children() -> None:
    a = AlwaysOnSponsor(name="a", ticks=3)
    b = AlwaysOnSponsor(name="b", ticks=5)
    c = AlwaysOnSponsor(name="c", ticks=7)

    ab_then_c = AllOf(AllOf(a, b), c)
    a_then_bc = AllOf(a, AllOf(b, c))

    d1 = ab_then_c.propose_lease(_ctx(), prior=None)
    d2 = a_then_bc.propose_lease(_ctx(), prior=None)
    assert isinstance(d1, Continue) and isinstance(d2, Continue)
    assert d1.lease.ticks == d2.lease.ticks == 3


def test_any_of_is_associative_for_bounded_children() -> None:
    a = AlwaysOnSponsor(name="a", ticks=3)
    b = AlwaysOnSponsor(name="b", ticks=5)
    c = AlwaysOnSponsor(name="c", ticks=7)

    ab_then_c = AnyOf(AnyOf(a, b), c)
    a_then_bc = AnyOf(a, AnyOf(b, c))

    d1 = ab_then_c.propose_lease(_ctx(), prior=None)
    d2 = a_then_bc.propose_lease(_ctx(), prior=None)
    assert isinstance(d1, Continue) and isinstance(d2, Continue)
    assert d1.lease.ticks == d2.lease.ticks == 7

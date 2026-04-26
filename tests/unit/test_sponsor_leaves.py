from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from quadro.sponsor import (
    BoardEventBudgetSponsor,
    CallableSponsor,
    Continue,
    DeadlineSponsor,
    Drain,
    GoalSponsor,
    Lease,
    LlmTokenBudgetSponsor,
    MeterReadings,
    QueueDepthSponsor,
    SponsorContext,
    Stop,
    TickBudgetSponsor,
    WorkerBudgetSponsor,
)


def _ctx(
    *,
    state: dict | None = None,
    meters: MeterReadings | None = None,
    now: datetime | None = None,
    history: tuple[Lease, ...] = (),
) -> SponsorContext:
    return SponsorContext(
        state=state or {"tasks": [], "agents": [], "data": {}},
        chief_telemetry={},
        meters=meters or MeterReadings(),
        lease_history=history,
        now=now or datetime.now(timezone.utc),
    )


# ── GoalSponsor ────────────────────────────────────────────────────────────────


def test_goal_sponsor_continue_when_predicate_false_and_stop_when_true() -> None:
    hits = 0

    def pred(state: dict) -> bool:
        nonlocal hits
        hits += 1
        return hits > 2

    sponsor = GoalSponsor(pred)

    d1 = sponsor.propose_lease(_ctx(), prior=None)
    d2 = sponsor.propose_lease(_ctx(), prior=None)
    d3 = sponsor.propose_lease(_ctx(), prior=None)

    assert isinstance(d1, Continue) and d1.lease.ticks == 1
    assert isinstance(d2, Continue)
    assert isinstance(d3, Stop)


def test_goal_sponsor_probe_ticks_is_relative_to_current_readings() -> None:
    sponsor = GoalSponsor(lambda s: False, probe_ticks=5)
    ctx = _ctx(meters=MeterReadings(ticks=10))
    decision = sponsor.propose_lease(ctx, prior=None)
    assert isinstance(decision, Continue)
    assert decision.lease.ticks == 15  # 10 + 5


def test_goal_sponsor_probe_ticks_none_yields_unbounded_axis() -> None:
    sponsor = GoalSponsor(lambda s: False, probe_ticks=None)
    decision = sponsor.propose_lease(_ctx(), prior=None)
    assert isinstance(decision, Continue)
    assert decision.lease.ticks is None


# ── DeadlineSponsor ───────────────────────────────────────────────────────────


def test_deadline_sponsor_continue_before_and_stop_after() -> None:
    now = datetime(2030, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    deadline = now + timedelta(minutes=5)
    sponsor = DeadlineSponsor(deadline)

    d1 = sponsor.propose_lease(_ctx(now=now), prior=None)
    d2 = sponsor.propose_lease(_ctx(now=deadline + timedelta(seconds=1)), prior=None)

    assert isinstance(d1, Continue)
    assert d1.lease.deadline == deadline
    assert isinstance(d2, Stop)
    assert "deadline_passed" in d2.reason


def test_deadline_sponsor_from_now_uses_timedelta_kwargs() -> None:
    sponsor = DeadlineSponsor.from_now(minutes=10)
    d = sponsor.propose_lease(_ctx(), prior=None)
    assert isinstance(d, Continue)
    assert d.lease.deadline is not None


# ── TickBudgetSponsor ─────────────────────────────────────────────────────────


def test_tick_budget_stops_when_exhausted() -> None:
    sponsor = TickBudgetSponsor(10)

    d1 = sponsor.propose_lease(_ctx(meters=MeterReadings(ticks=0)), prior=None)
    d2 = sponsor.propose_lease(_ctx(meters=MeterReadings(ticks=10)), prior=None)

    assert isinstance(d1, Continue)
    assert d1.lease.ticks == 10
    assert isinstance(d2, Stop)


def test_tick_budget_lease_ceiling_stays_stable_across_renewals() -> None:
    sponsor = TickBudgetSponsor(100)
    d1 = sponsor.propose_lease(_ctx(meters=MeterReadings(ticks=30)), prior=None)
    assert isinstance(d1, Continue)
    # The lease's tick axis is an absolute ceiling. For TickBudget it is
    # used + remaining, i.e. the budget total.
    assert d1.lease.ticks == 100


def test_tick_budget_rejects_non_positive() -> None:
    with pytest.raises(ValueError):
        TickBudgetSponsor(0)


# ── WorkerBudget / LlmTokenBudget / BoardEventBudget ──────────────────────────


def test_worker_budget_single_axis() -> None:
    sponsor = WorkerBudgetSponsor(5)
    d1 = sponsor.propose_lease(
        _ctx(meters=MeterReadings(worker_invocations=2)), prior=None
    )
    d2 = sponsor.propose_lease(
        _ctx(meters=MeterReadings(worker_invocations=5)), prior=None
    )
    assert isinstance(d1, Continue) and d1.lease.worker_invocations == 5
    assert isinstance(d2, Stop)


def test_llm_token_budget_single_axis() -> None:
    sponsor = LlmTokenBudgetSponsor(10_000)
    d = sponsor.propose_lease(_ctx(meters=MeterReadings(llm_tokens=500)), prior=None)
    assert isinstance(d, Continue)
    assert d.lease.llm_tokens == 10_000


def test_board_event_budget_single_axis() -> None:
    sponsor = BoardEventBudgetSponsor(50)
    d = sponsor.propose_lease(_ctx(meters=MeterReadings(board_events=49)), prior=None)
    assert isinstance(d, Continue)
    assert d.lease.board_events == 50
    d2 = sponsor.propose_lease(_ctx(meters=MeterReadings(board_events=50)), prior=None)
    assert isinstance(d2, Stop)


# ── CallableSponsor ───────────────────────────────────────────────────────────


def test_callable_sponsor_invokes_callable_and_patches_lease() -> None:
    def fn(ctx, prior):
        return Continue(lease=Lease(ticks=3))

    sponsor = CallableSponsor(fn, name="adhoc")
    prior = Lease(id="prev01")
    now = datetime(2030, 3, 1, tzinfo=timezone.utc)
    d = sponsor.propose_lease(_ctx(now=now), prior=prior)
    assert isinstance(d, Continue)
    assert d.lease.ticks == 3
    assert d.lease.renewal_of == "prev01"
    assert d.lease.source == "adhoc"


def test_callable_sponsor_passes_through_stop() -> None:
    sponsor = CallableSponsor(lambda ctx, prior: Stop(reason="custom"))
    d = sponsor.propose_lease(_ctx(), prior=None)
    assert isinstance(d, Stop)
    assert d.reason == "custom"


# ── QueueDepthSponsor ─────────────────────────────────────────────────────────


def test_queue_depth_continue_when_backlog_present() -> None:
    sponsor = QueueDepthSponsor("orders_in_queue", min_depth=1)
    state = {"tasks": [], "agents": [], "data": {"orders_in_queue": [1, 2, 3]}}
    d = sponsor.propose_lease(_ctx(state=state), prior=None)
    assert isinstance(d, Continue)
    assert "queue_depth:3" in d.lease.reason


def test_queue_depth_drain_when_empty() -> None:
    sponsor = QueueDepthSponsor("orders_in_queue", min_depth=1)
    state = {"tasks": [], "agents": [], "data": {"orders_in_queue": []}}
    d = sponsor.propose_lease(_ctx(state=state), prior=None)
    assert isinstance(d, Drain)
    assert "queue_empty" in d.reason


def test_queue_depth_immediate_stop_option() -> None:
    sponsor = QueueDepthSponsor("orders_in_queue", min_depth=1, immediate_stop=True)
    state = {"tasks": [], "agents": [], "data": {}}
    d = sponsor.propose_lease(_ctx(state=state), prior=None)
    assert isinstance(d, Stop)


def test_queue_depth_accepts_non_list_values_as_zero() -> None:
    sponsor = QueueDepthSponsor("key")
    state = {"data": {"key": 42}, "tasks": [], "agents": []}
    d = sponsor.propose_lease(_ctx(state=state), prior=None)
    assert isinstance(d, Drain)

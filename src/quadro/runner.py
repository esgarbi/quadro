"""RunLoop — the Sponsor-governed poll loop.

The :class:`RunLoop` consults a :class:`~quadro.sponsor.Sponsor` for
authority over when to keep working, when to drain, and when to stop. It
owns the :class:`~quadro.sponsor.meters.MeterBundle` and wires it to the
Chief and board so that every Sponsor consultation sees live readings.

Authority axes supported by the lease: poll ticks, wall-clock deadline,
worker invocations, LLM tokens, board events. See ``docs/design/sponsor.md``
for the full contract.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from .agents.chief import ChiefAgent
from .board.client import BoardClient
from .dispatch import DRAIN_FLAG_KEY
from .sponsor.meters import MeterBundle
from .sponsor.types import (
    Continue,
    Drain,
    Lease,
    LeaseDecision,
    Sponsor,
    SponsorContext,
    Stop,
)

logger = logging.getLogger(__name__)

_DEFAULT_POLL_INTERVAL = 3.0  # seconds; examples use 0.0 for speed
_DEFAULT_OMBUDSMAN_INTERVAL = 30.0  # seconds
_DEFAULT_DRAIN_MAX_DURATION = timedelta(minutes=5)
_SPONSOR_LOG_KEY = "_sponsor_log"
_SPONSOR_STATUS_KEY = "_sponsor_status"
_SPONSOR_LOG_LIMIT = 200


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class RunLoop:
    """Drive a Sponsor-governed run to completion.

    The loop structure is:

    1. Seed the chief, reset meters, ask the Sponsor for the first Lease.
    2. On each tick: update meters, check for axis exhaustion, re-consult
       the Sponsor if needed, run user ``on_cycle`` callback, handle drain
       transitions, fire ombudsman nudges on schedule.
    3. On Stop (sponsor-issued or drain-completed): publish the final state
       and exit.

    Example::

        state = (
            RunLoop(board_client, chief)
            .sponsor(AllOf(GoalSponsor(...), TickBudgetSponsor(500)))
            .on_cycle(log_status)
            .run()
        )
    """

    def __init__(
        self, board_or_client: BoardClient | QuadroBoard, chief: ChiefAgent
    ) -> None:
        from .board.board import QuadroBoard as _QuadroBoard

        if isinstance(board_or_client, _QuadroBoard):
            self._board = board_or_client
            self._board_client = board_or_client.client()
        else:
            self._board = None
            self._board_client = board_or_client
        self._chief = chief
        self._sponsor: Sponsor | None = None
        self._cycle_callback: Callable[[dict, int], None] | None = None
        self._complete_callback: Callable[[dict], None] | None = None
        self._poll_interval = _DEFAULT_POLL_INTERVAL
        self._ombudsman_interval = _DEFAULT_OMBUDSMAN_INTERVAL
        self._drain_max_duration = _DEFAULT_DRAIN_MAX_DURATION
        self._ombudsman_instance = None

        self._meters = MeterBundle()
        self._lease_history: list[Lease] = []

    # ── Builder methods ───────────────────────────────────────────────────────

    def sponsor(self, sponsor: Sponsor) -> RunLoop:
        """Install the Sponsor that governs the run's lifetime."""
        self._sponsor = sponsor
        return self

    def on_cycle(self, callback: Callable[[dict, int], None]) -> RunLoop:
        self._cycle_callback = callback
        return self

    def on_complete(self, callback: Callable[[dict], None]) -> RunLoop:
        self._complete_callback = callback
        return self

    def poll_every(self, seconds: float) -> RunLoop:
        self._poll_interval = seconds
        return self

    def ombudsman_every(self, seconds: float) -> RunLoop:
        self._ombudsman_interval = seconds
        return self

    def drain_max_duration(self, td: timedelta) -> RunLoop:
        self._drain_max_duration = td
        return self

    def ombudsman(self, ombudsman_instance: Any) -> RunLoop:
        """Optional Ombudsman instance. nudge() is called alongside the chief nudge."""
        self._ombudsman_instance = ombudsman_instance
        return self

    @property
    def meters(self) -> MeterBundle:
        return self._meters

    @property
    def lease_history(self) -> tuple[Lease, ...]:
        return tuple(self._lease_history)

    # ── Run ───────────────────────────────────────────────────────────────────

    def run(self) -> dict:
        if self._sponsor is None:
            raise ValueError("RunLoop requires .sponsor(sponsor) before .run()")

        self._meters.reset()
        self._lease_history.clear()
        self._set_drain_flag(False)
        self._attach_subscribers()

        try:
            return self._run_inner()
        finally:
            self._detach_subscribers()
            self._set_drain_flag(False)

    # ── Internals ─────────────────────────────────────────────────────────────

    def _attach_subscribers(self) -> None:
        self._chief.add_wake_listener(self._meters.on_chief_wake)
        if self._board is not None:
            self._board.add_event_listener(self._meters.on_board_event)

    def _detach_subscribers(self) -> None:
        self._chief.remove_wake_listener(self._meters.on_chief_wake)
        if self._board is not None:
            self._board.remove_event_listener(self._meters.on_board_event)

    def _set_drain_flag(self, value: bool) -> None:
        try:
            self._board_client.put_data(DRAIN_FLAG_KEY, bool(value))
        except Exception:  # noqa: BLE001
            pass
        try:
            self._chief.set_draining(bool(value))
        except Exception:  # noqa: BLE001
            pass
        self._publish_status(draining=bool(value))

    def _publish_status(
        self,
        *,
        active_lease: Lease | None = None,
        draining: bool | None = None,
        drain_deadline: datetime | None = None,
        final: bool = False,
    ) -> None:
        """Publish the current lease + drain snapshot for the UI and dashboards."""
        try:
            existing = self._board_client.get_data(_SPONSOR_STATUS_KEY) or {}
            if not isinstance(existing, dict):
                existing = {}
        except Exception:  # noqa: BLE001
            existing = {}
        status = dict(existing)
        if active_lease is not None:
            status["active_lease"] = active_lease.to_dict()
        elif final:
            status["active_lease"] = None
        if draining is not None:
            status["draining"] = bool(draining)
        if drain_deadline is not None:
            status["drain_deadline"] = drain_deadline.isoformat()
        elif draining is False:
            status["drain_deadline"] = None
        status["sponsor_id"] = getattr(
            self._sponsor, "name", type(self._sponsor).__name__ if self._sponsor else None
        )
        status["meters"] = self._meters.snapshot().to_dict()
        status["updated_at"] = _utc_now().isoformat()
        try:
            self._board_client.put_data(_SPONSOR_STATUS_KEY, status)
        except Exception:  # noqa: BLE001
            logger.debug("Failed to publish sponsor status", exc_info=True)

    def _current_state(self) -> dict:
        return self._board_client.full_state()

    def _chief_telemetry(self, state: dict) -> dict:
        data = state.get("data") or {}
        telem = data.get("_chief_telemetry") or {}
        return telem if isinstance(telem, dict) else {}

    def _make_context(self, state: dict) -> SponsorContext:
        return SponsorContext(
            state=state,
            chief_telemetry=self._chief_telemetry(state),
            meters=self._meters.snapshot(),
            lease_history=tuple(self._lease_history),
            now=_utc_now(),
        )

    def _consult_sponsor(self, prior: Lease | None, state: dict) -> LeaseDecision:
        ctx = self._make_context(state)
        sponsor = self._sponsor
        assert sponsor is not None
        try:
            decision = sponsor.propose_lease(ctx, prior)
        except Exception as exc:  # noqa: BLE001
            if getattr(sponsor, "fail_open", False) and prior is not None:
                logger.warning(
                    "Sponsor %s raised; fail_open=True, renewing prior: %s",
                    getattr(sponsor, "name", type(sponsor).__name__),
                    exc,
                )
                from dataclasses import replace

                decision = Continue(
                    lease=replace(prior, reason=f"sponsor_fail_open:{exc}"),
                    reason=f"sponsor_fail_open:{exc}",
                )
            else:
                logger.exception("Sponsor raised; treating as Stop")
                decision = Stop(reason=f"sponsor_error:{exc}")

        if isinstance(decision, Continue):
            decision = Continue(
                lease=decision.lease.clamp(), reason=decision.reason
            )
        self._log_decision(decision, prior)
        return decision

    def _log_decision(
        self, decision: LeaseDecision, prior: Lease | None
    ) -> None:
        sponsor = self._sponsor
        sponsor_id = getattr(sponsor, "name", type(sponsor).__name__)
        record: dict[str, Any] = {
            "at": _utc_now().isoformat(),
            "sponsor_id": sponsor_id,
            "prior_lease_id": prior.id if prior is not None else None,
            "meters": self._meters.snapshot().to_dict(),
        }
        if isinstance(decision, Continue):
            record["decision"] = "continue"
            record["reason"] = decision.reason
            record["lease"] = decision.lease.to_dict()
        elif isinstance(decision, Drain):
            record["decision"] = "drain"
            record["reason"] = decision.reason
            record["deadline"] = (
                decision.deadline.isoformat() if decision.deadline else None
            )
        else:
            record["decision"] = "stop"
            record["reason"] = decision.reason

        try:
            existing = self._board_client.get_data(_SPONSOR_LOG_KEY) or []
            if not isinstance(existing, list):
                existing = []
            existing.append(record)
            if len(existing) > _SPONSOR_LOG_LIMIT:
                existing = existing[-_SPONSOR_LOG_LIMIT:]
            self._board_client.put_data(_SPONSOR_LOG_KEY, existing)
        except Exception:  # noqa: BLE001
            logger.debug("Failed to persist sponsor log", exc_info=True)

    def _lease_exhausted(self, lease: Lease, state: dict) -> bool:
        now = _utc_now()
        readings = self._meters.snapshot()
        if lease.deadline is not None and now >= lease.deadline:
            return True
        if lease.ticks is not None and readings.ticks >= lease.ticks:
            return True
        if (
            lease.worker_invocations is not None
            and readings.worker_invocations >= lease.worker_invocations
        ):
            return True
        if lease.llm_tokens is not None and readings.llm_tokens >= lease.llm_tokens:
            return True
        if (
            lease.board_events is not None
            and readings.board_events >= lease.board_events
        ):
            return True
        return False

    def _has_active_tasks(self, state: dict) -> bool:
        """True if any task is in a non-terminal, non-pending-handoff status.

        During drain, once this is False, auto-Stop fires.
        """
        terminal = {
            str(s) for s in state.get("_terminal_statuses", [])
        } or {"COMPLETE", "HUMAN_REVIEW", "ON_HOLD"}
        pending_ok_to_end: set[str] = {"UNASSIGNED"}
        end_statuses = terminal | pending_ok_to_end
        for t in state.get("tasks", []):
            if str(t["status"]) not in end_statuses:
                return False
        return True

    def _run_inner(self) -> dict:
        logger.debug("RunLoop: seeding chief")
        self._chief.nudge(trigger="seed")

        state = self._current_state()
        decision = self._consult_sponsor(prior=None, state=state)

        if isinstance(decision, Stop):
            return self._finalise(state)

        active_lease: Lease | None = None
        drain_deadline: datetime | None = None
        draining = False

        if isinstance(decision, Continue):
            active_lease = decision.lease
            self._lease_history.append(active_lease)
            self._publish_status(active_lease=active_lease, draining=False)
        elif isinstance(decision, Drain):
            draining = True
            drain_deadline = self._resolve_drain_deadline(decision.deadline)
            self._set_drain_flag(True)
            self._publish_status(draining=True, drain_deadline=drain_deadline)

        last_ombudsman = time.monotonic()
        cycle = 0
        final_state: dict = state

        while True:
            time.sleep(self._poll_interval)
            self._meters.ticks.tick()

            state = self._current_state()
            final_state = state

            if self._cycle_callback is not None:
                try:
                    self._cycle_callback(state, cycle)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("RunLoop on_cycle error: %s", exc)

            # ── Drain lifecycle ──────────────────────────────────────────────
            if draining:
                if self._has_active_tasks(state):
                    logger.info("RunLoop: drain complete (no active tasks)")
                    return self._finalise(state)
                if drain_deadline is not None and _utc_now() >= drain_deadline:
                    logger.info("RunLoop: drain deadline hit; forcing stop")
                    return self._finalise(state)
                cycle += 1
                self._maybe_nudge_ombudsman(last_ombudsman)
                last_ombudsman = self._advance_ombudsman_clock(last_ombudsman)
                continue

            # ── Normal (non-drain) lifecycle ─────────────────────────────────
            if active_lease is None or self._lease_exhausted(active_lease, state):
                decision = self._consult_sponsor(prior=active_lease, state=state)
                if isinstance(decision, Stop):
                    return self._finalise(state)
                if isinstance(decision, Drain):
                    draining = True
                    drain_deadline = self._resolve_drain_deadline(decision.deadline)
                    self._set_drain_flag(True)
                    self._publish_status(
                        draining=True, drain_deadline=drain_deadline
                    )
                    cycle += 1
                    self._maybe_nudge_ombudsman(last_ombudsman)
                    last_ombudsman = self._advance_ombudsman_clock(last_ombudsman)
                    continue
                assert isinstance(decision, Continue)
                active_lease = decision.lease
                self._lease_history.append(active_lease)
                self._publish_status(active_lease=active_lease, draining=False)

            cycle += 1
            self._maybe_nudge_ombudsman(last_ombudsman)
            last_ombudsman = self._advance_ombudsman_clock(last_ombudsman)

        # Unreachable — loop returns via _finalise.

    def _resolve_drain_deadline(
        self, lease_deadline: datetime | None
    ) -> datetime | None:
        if lease_deadline is not None:
            return lease_deadline
        if self._drain_max_duration is None:
            return None
        return _utc_now() + self._drain_max_duration

    def _maybe_nudge_ombudsman(self, last: float) -> None:
        if self._ombudsman_interval <= 0:
            self._chief.nudge(trigger="ombudsman")
            if self._ombudsman_instance is not None:
                self._ombudsman_instance.nudge()
            return
        now = time.monotonic()
        if now - last >= self._ombudsman_interval:
            logger.debug("RunLoop ombudsman: nudging chief")
            self._chief.nudge(trigger="ombudsman")
            if self._ombudsman_instance is not None:
                self._ombudsman_instance.nudge()

    def _advance_ombudsman_clock(self, last: float) -> float:
        now = time.monotonic()
        if self._ombudsman_interval <= 0 or now - last >= self._ombudsman_interval:
            return now
        return last

    def _finalise(self, state: dict) -> dict:
        self._publish_status(
            active_lease=None, draining=False, drain_deadline=None, final=True
        )
        try:
            state = self._current_state()
        except Exception:  # noqa: BLE001
            pass
        if self._complete_callback is not None:
            try:
                self._complete_callback(state)
            except Exception as exc:  # noqa: BLE001
                logger.warning("RunLoop on_complete error: %s", exc)
        return state

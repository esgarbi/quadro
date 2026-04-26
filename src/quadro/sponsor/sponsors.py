"""Built-in Sponsor implementations.

Leaf Sponsors (local, zero-dependency):

- :class:`GoalSponsor`
- :class:`DeadlineSponsor`
- :class:`TickBudgetSponsor`
- :class:`WorkerBudgetSponsor`
- :class:`LlmTokenBudgetSponsor`
- :class:`BoardEventBudgetSponsor`
- :class:`CallableSponsor`
- :class:`QueueDepthSponsor`

External Sponsors (plug into remote/async decision sources):

- :class:`HttpSponsor`       — polls an HTTP endpoint.
- :class:`CallbackSponsor`   — wraps an async callable.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib import request as urllib_request
from urllib.error import HTTPError, URLError

from .types import (
    Continue,
    Drain,
    Lease,
    LeaseDecision,
    Sponsor,
    SponsorContext,
    Stop,
    new_lease_id,
)

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _lease(
    *,
    source: str,
    prior: Lease | None,
    ticks: int | None = None,
    deadline: datetime | None = None,
    worker_invocations: int | None = None,
    llm_tokens: int | None = None,
    board_events: int | None = None,
    reason: str = "",
    issued_at: datetime | None = None,
) -> Lease:
    """Helper to mint a Lease with consistent renewal-chaining."""
    return Lease(
        id=new_lease_id(),
        issued_at=issued_at or _utc_now(),
        ticks=ticks,
        deadline=deadline,
        worker_invocations=worker_invocations,
        llm_tokens=llm_tokens,
        board_events=board_events,
        source=source,
        reason=reason,
        renewal_of=prior.id if prior is not None else None,
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  GoalSponsor — canonical replacement for the old `done_when` predicate
# ═══════════════════════════════════════════════════════════════════════════════


class GoalSponsor:
    """Continue while a goal predicate is false; Stop when it becomes true.

    The predicate is evaluated against the board's full state on every
    consultation. Because the runtime only consults on startup and on axis
    exhaustion, :class:`GoalSponsor` is usually composed with a
    :class:`TickBudgetSponsor` or :class:`DeadlineSponsor` that provides the
    cadence for re-evaluation — otherwise the runtime only re-asks when
    drain-complete, which is not usually what you want.

    The ``probe_ticks`` kwarg is a convenience: it sets the issued Lease's
    ``ticks`` axis so the runtime re-consults every N ticks without needing
    a sibling sponsor. Default 1 so the goal is re-checked at each tick,
    matching the semantics of the old ``done_when``.

    Example::

        sponsor = GoalSponsor(lambda s: count_done(s) >= 10)
    """

    def __init__(
        self,
        predicate: Callable[[dict], bool],
        *,
        name: str = "goal",
        probe_ticks: int | None = 1,
        reason: str = "goal_not_met",
        done_reason: str = "goal_met",
        fail_open: bool = False,
    ) -> None:
        self.name = name
        self.fail_open = fail_open
        self._predicate = predicate
        self._probe_ticks = probe_ticks
        self._reason = reason
        self._done_reason = done_reason

    def propose_lease(self, ctx: SponsorContext, prior: Lease | None) -> LeaseDecision:
        if self._predicate(ctx.state):
            return Stop(reason=self._done_reason)
        ticks_ceiling = (
            ctx.meters.ticks + self._probe_ticks
            if self._probe_ticks is not None
            else None
        )
        lease = _lease(
            source=self.name,
            prior=prior,
            ticks=ticks_ceiling,
            reason=self._reason,
            issued_at=ctx.now,
        )
        return Continue(lease=lease, reason=self._reason)


# ═══════════════════════════════════════════════════════════════════════════════
#  DeadlineSponsor
# ═══════════════════════════════════════════════════════════════════════════════


class DeadlineSponsor:
    """Continue until a wall-clock deadline, then Stop.

    Construct with an explicit UTC datetime, or use
    :meth:`from_now` with ``timedelta`` kwargs.
    """

    def __init__(
        self,
        deadline: datetime,
        *,
        name: str = "deadline",
        fail_open: bool = False,
    ) -> None:
        self.name = name
        self.fail_open = fail_open
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        self._deadline = deadline

    @classmethod
    def from_now(
        cls,
        *,
        name: str = "deadline",
        fail_open: bool = False,
        **kwargs: Any,
    ) -> "DeadlineSponsor":
        """Construct using ``timedelta`` kwargs: seconds, minutes, hours, days."""
        td = timedelta(**kwargs)
        return cls(_utc_now() + td, name=name, fail_open=fail_open)

    def propose_lease(self, ctx: SponsorContext, prior: Lease | None) -> LeaseDecision:
        if ctx.now >= self._deadline:
            return Stop(reason=f"deadline_passed:{self._deadline.isoformat()}")
        lease = _lease(
            source=self.name,
            prior=prior,
            deadline=self._deadline,
            reason=f"deadline:{self._deadline.isoformat()}",
            issued_at=ctx.now,
        )
        return Continue(lease=lease, reason=lease.reason)


# ═══════════════════════════════════════════════════════════════════════════════
#  Budget sponsors — single-axis absolute budgets
# ═══════════════════════════════════════════════════════════════════════════════


class _SingleAxisBudgetSponsor:
    """Base class for axis-bounded budget sponsors.

    Subclasses set ``_axis`` (one of "ticks"/"worker_invocations"/"llm_tokens"/
    "board_events") and implement ``_meter_value`` to read the right axis off
    ``MeterReadings``.
    """

    _axis: str = ""

    def __init__(
        self,
        total: int,
        *,
        name: str,
        fail_open: bool = False,
    ) -> None:
        if total <= 0:
            raise ValueError(f"{type(self).__name__} requires total > 0; got {total}")
        self.name = name
        self.fail_open = fail_open
        self._total = total

    def _meter_value(self, readings) -> int:  # pragma: no cover - overridden
        raise NotImplementedError

    def propose_lease(self, ctx: SponsorContext, prior: Lease | None) -> LeaseDecision:
        used = self._meter_value(ctx.meters)
        remaining = self._total - used
        if remaining <= 0:
            return Stop(reason=f"{self._axis}_budget_exhausted:{used}/{self._total}")
        # The lease expresses the *remaining* budget on this axis.
        # The runtime's exhaustion check compares meter readings against the
        # lease's axis value, treating it as an absolute ceiling from the
        # issue point. We express it that way by snapshotting used + remaining
        # at issue time.
        ceiling = used + remaining
        kwargs: dict[str, Any] = {self._axis: ceiling}
        lease = _lease(
            source=self.name,
            prior=prior,
            reason=f"{self._axis}_budget:{used}/{self._total}",
            issued_at=ctx.now,
            **kwargs,
        )
        return Continue(lease=lease, reason=lease.reason)


class TickBudgetSponsor(_SingleAxisBudgetSponsor):
    """Continue for up to N poll ticks. Parity replacement for ``max_cycles=N``."""

    _axis = "ticks"

    def __init__(
        self, total: int, *, name: str = "tick_budget", fail_open: bool = False
    ) -> None:
        super().__init__(total, name=name, fail_open=fail_open)

    def _meter_value(self, readings) -> int:
        return readings.ticks


class WorkerBudgetSponsor(_SingleAxisBudgetSponsor):
    """Continue while cumulative worker invocations are below N."""

    _axis = "worker_invocations"

    def __init__(
        self, total: int, *, name: str = "worker_budget", fail_open: bool = False
    ) -> None:
        super().__init__(total, name=name, fail_open=fail_open)

    def _meter_value(self, readings) -> int:
        return readings.worker_invocations


class LlmTokenBudgetSponsor(_SingleAxisBudgetSponsor):
    """Continue while cumulative LLM tokens are below N."""

    _axis = "llm_tokens"

    def __init__(
        self, total: int, *, name: str = "llm_token_budget", fail_open: bool = False
    ) -> None:
        super().__init__(total, name=name, fail_open=fail_open)

    def _meter_value(self, readings) -> int:
        return readings.llm_tokens


class BoardEventBudgetSponsor(_SingleAxisBudgetSponsor):
    """Continue while cumulative board events are below N."""

    _axis = "board_events"

    def __init__(
        self, total: int, *, name: str = "board_event_budget", fail_open: bool = False
    ) -> None:
        super().__init__(total, name=name, fail_open=fail_open)

    def _meter_value(self, readings) -> int:
        return readings.board_events


# ═══════════════════════════════════════════════════════════════════════════════
#  CallableSponsor — user lambda returning a LeaseDecision
# ═══════════════════════════════════════════════════════════════════════════════


class CallableSponsor:
    """Wrap a plain callable ``(ctx, prior) -> LeaseDecision``.

    For quick ad-hoc policies without subclassing. The callable is called
    synchronously at consultation time; use :class:`CallbackSponsor` for
    async needs.
    """

    def __init__(
        self,
        fn: Callable[[SponsorContext, Lease | None], LeaseDecision],
        *,
        name: str = "callable",
        fail_open: bool = False,
    ) -> None:
        self.name = name
        self.fail_open = fail_open
        self._fn = fn

    def propose_lease(self, ctx: SponsorContext, prior: Lease | None) -> LeaseDecision:
        decision = self._fn(ctx, prior)
        if isinstance(decision, Continue):
            lease = decision.lease
            source = (
                lease.source
                if lease.source and lease.source != "anonymous"
                else self.name
            )
            patched = Lease(
                id=lease.id or new_lease_id(),
                issued_at=lease.issued_at or ctx.now,
                ticks=lease.ticks,
                deadline=lease.deadline,
                worker_invocations=lease.worker_invocations,
                llm_tokens=lease.llm_tokens,
                board_events=lease.board_events,
                source=source,
                reason=lease.reason or decision.reason,
                renewal_of=(
                    prior.id
                    if prior is not None and lease.renewal_of is None
                    else lease.renewal_of
                ),
            )
            return Continue(lease=patched, reason=decision.reason)
        return decision


# ═══════════════════════════════════════════════════════════════════════════════
#  QueueDepthSponsor — continue while a board data key has backlog
# ═══════════════════════════════════════════════════════════════════════════════


class QueueDepthSponsor:
    """Continue while the named board data key has at least ``min_depth`` entries.

    The data key is looked up off ``ctx.state["data"]``. The value is expected
    to be a list-like; non-list values are treated as zero-depth. When depth
    drops below ``min_depth``, the Sponsor returns ``Drain`` (lets in-flight
    work finish) rather than an immediate ``Stop`` — this matches the
    intuition that the queue having emptied is a "graceful wind-down" signal,
    not an emergency brake.

    Use ``immediate_stop=True`` to return ``Stop`` directly.
    """

    def __init__(
        self,
        key: str,
        *,
        min_depth: int = 1,
        name: str = "queue_depth",
        probe_ticks: int | None = 1,
        drain_deadline: datetime | None = None,
        immediate_stop: bool = False,
        fail_open: bool = False,
    ) -> None:
        self.name = name
        self.fail_open = fail_open
        self._key = key
        self._min_depth = max(0, min_depth)
        self._probe_ticks = probe_ticks
        self._drain_deadline = drain_deadline
        self._immediate_stop = immediate_stop

    def _depth(self, state: dict) -> int:
        data = state.get("data") or {}
        value = data.get(self._key)
        if isinstance(value, list):
            return len(value)
        if isinstance(value, (dict, set, tuple)):
            return len(value)
        return 0

    def propose_lease(self, ctx: SponsorContext, prior: Lease | None) -> LeaseDecision:
        depth = self._depth(ctx.state)
        if depth >= self._min_depth:
            ticks_ceiling = (
                ctx.meters.ticks + self._probe_ticks
                if self._probe_ticks is not None
                else None
            )
            lease = _lease(
                source=self.name,
                prior=prior,
                ticks=ticks_ceiling,
                reason=f"queue_depth:{depth}>={self._min_depth}",
                issued_at=ctx.now,
            )
            return Continue(lease=lease, reason=lease.reason)

        if self._immediate_stop:
            return Stop(reason=f"queue_empty:{self._key}")
        return Drain(
            deadline=self._drain_deadline,
            reason=f"queue_empty:{self._key}",
        )


# ═══════════════════════════════════════════════════════════════════════════════
#  External Sponsors
# ═══════════════════════════════════════════════════════════════════════════════


class CallbackSponsor:
    """Wrap an async callable returning a :class:`LeaseDecision`.

    Useful for in-process integrations with orchestration engines (Temporal,
    Dagster, Prefect) that expose async APIs. The callable receives the same
    ``(ctx, prior)`` pair and must return a ``LeaseDecision``.

    The Sponsor is synchronous externally (the ``propose_lease`` signature
    must stay sync to match :class:`Sponsor`), so it runs the coroutine on
    the current event loop if one exists or spins up a temporary loop.
    """

    def __init__(
        self,
        callback: Callable[[SponsorContext, Lease | None], Awaitable[LeaseDecision]],
        *,
        name: str = "callback",
        timeout: float | None = 30.0,
        fail_open: bool = False,
    ) -> None:
        self.name = name
        self.fail_open = fail_open
        self._callback = callback
        self._timeout = timeout

    def propose_lease(self, ctx: SponsorContext, prior: Lease | None) -> LeaseDecision:
        coro = self._callback(ctx, prior)
        if self._timeout is not None:

            async def _bounded() -> LeaseDecision:
                return await asyncio.wait_for(coro, timeout=self._timeout)

            runner = _bounded()
        else:
            runner = coro

        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None

        if running_loop is not None:
            future = asyncio.run_coroutine_threadsafe(runner, running_loop)
            return future.result(timeout=self._timeout if self._timeout else None)
        return asyncio.run(runner)


class HttpSponsor:
    """Poll an HTTP endpoint for a ``LeaseDecision``.

    The endpoint must accept a POST with ``Content-Type: application/json``
    and respond 200 with a JSON body of the shape::

        {"decision": "continue" | "drain" | "stop",
         "reason": "...",
         "lease": {"ticks": 10, "deadline": "ISO8601", ...}}

    On any non-200 or network error the Sponsor returns ``Stop`` by default
    (fail-closed). Set ``fail_open=True`` to renew the previous lease on
    transient errors.

    Retry policy: exponential backoff with jitter up to ``max_retries``
    (default 2). Transient errors (timeout, connection refused, 5xx) are
    retried; 4xx is treated as a definitive signal and not retried.
    """

    def __init__(
        self,
        url: str,
        *,
        name: str = "http",
        timeout: float = 5.0,
        max_retries: int = 2,
        backoff: float = 0.25,
        fail_open: bool = False,
        request_payload: dict | None = None,
    ) -> None:
        self.name = name
        self.fail_open = fail_open
        self._url = url
        self._timeout = timeout
        self._max_retries = max(0, max_retries)
        self._backoff = max(0.0, backoff)
        self._extra_payload = request_payload or {}

    # ── Internals ─────────────────────────────────────────────────────────────

    def _post_json(self, body: dict) -> tuple[int, dict]:
        data = json.dumps(body).encode("utf-8")
        req = urllib_request.Request(
            self._url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib_request.urlopen(req, timeout=self._timeout) as resp:
            status = resp.status
            raw = resp.read().decode("utf-8") or "{}"
        parsed = json.loads(raw) if raw else {}
        return status, parsed

    def _build_body(self, ctx: SponsorContext, prior: Lease | None) -> dict:
        return {
            "now": ctx.now.isoformat(),
            "prior_lease_id": prior.id if prior is not None else None,
            "meters": ctx.meters.to_dict(),
            "state_summary": {
                "task_count": len(ctx.state.get("tasks", [])),
                "agent_count": len(ctx.state.get("agents", [])),
            },
            **self._extra_payload,
        }

    def _parse_decision(
        self, ctx: SponsorContext, prior: Lease | None, body: dict
    ) -> LeaseDecision:
        kind = str(body.get("decision", "stop")).lower()
        reason = str(body.get("reason", ""))
        if kind == "stop":
            return Stop(reason=reason or "http_stop")
        if kind == "drain":
            deadline_raw = body.get("deadline")
            deadline = (
                datetime.fromisoformat(deadline_raw)
                if isinstance(deadline_raw, str) and deadline_raw
                else None
            )
            return Drain(deadline=deadline, reason=reason or "http_drain")
        if kind == "continue":
            lease_body = body.get("lease") or {}
            deadline_raw = lease_body.get("deadline")
            deadline = (
                datetime.fromisoformat(deadline_raw)
                if isinstance(deadline_raw, str) and deadline_raw
                else None
            )
            lease = _lease(
                source=self.name,
                prior=prior,
                ticks=lease_body.get("ticks"),
                deadline=deadline,
                worker_invocations=lease_body.get("worker_invocations"),
                llm_tokens=lease_body.get("llm_tokens"),
                board_events=lease_body.get("board_events"),
                reason=reason or "http_continue",
                issued_at=ctx.now,
            )
            return Continue(lease=lease, reason=reason or "http_continue")
        return Stop(reason=f"http_unknown_decision:{kind}")

    # ── Public API ────────────────────────────────────────────────────────────

    def propose_lease(self, ctx: SponsorContext, prior: Lease | None) -> LeaseDecision:
        body = self._build_body(ctx, prior)
        last_exc: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                status, parsed = self._post_json(body)
            except HTTPError as exc:
                last_exc = exc
                if 400 <= exc.code < 500:
                    # Definitive; do not retry
                    return self._handle_error(prior, exc)
            except (URLError, TimeoutError, ConnectionError, OSError) as exc:
                last_exc = exc
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
            else:
                if status == 200:
                    return self._parse_decision(ctx, prior, parsed)
                if 400 <= status < 500:
                    return self._handle_error(prior, RuntimeError(f"HTTP {status}"))
                # 5xx falls through to retry
                last_exc = RuntimeError(f"HTTP {status}")
            if attempt < self._max_retries:
                time.sleep(self._backoff * (2**attempt))

        assert last_exc is not None
        return self._handle_error(prior, last_exc)

    def _handle_error(self, prior: Lease | None, exc: Exception) -> LeaseDecision:
        if self.fail_open and prior is not None:
            logger.warning(
                "HttpSponsor %s error; fail_open=True, renewing prior lease: %s",
                self.name,
                exc,
            )
            renewed = Lease(
                id=new_lease_id(),
                issued_at=_utc_now(),
                ticks=prior.ticks,
                deadline=prior.deadline,
                worker_invocations=prior.worker_invocations,
                llm_tokens=prior.llm_tokens,
                board_events=prior.board_events,
                source=prior.source,
                reason=f"fail_open_renewal:{exc}",
                renewal_of=prior.id,
            )
            return Continue(lease=renewed, reason=renewed.reason)
        return Stop(reason=f"http_error:{exc}")


# Protocol sanity
_check: type[Sponsor] = Sponsor  # noqa: F841 — documented

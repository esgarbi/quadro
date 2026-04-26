"""Meters — observe cost/usage axes for the Sponsor layer.

Each meter tracks one axis of "work done since the run started". The
:class:`MeterBundle` owns one of each and is read by the runtime to populate
``SponsorContext.meters`` at consultation time.

Meters are intentionally passive: they accept callbacks from other parts of
the system (Chief, LLM adapter, board event subscriber) rather than
polling. This keeps the hot paths cheap and keeps the Sponsor layer from
being a hidden bottleneck.

Axes:

- :class:`TickMeter`           — poll-tick counter (runtime increments).
- :class:`WallClockMeter`      — wall-clock elapsed since start.
- :class:`WorkerInvocationMeter` — count of worker wake events at the Chief.
- :class:`LlmTokenMeter`       — LLM tokens consumed. Updated via ``report``.
- :class:`BoardEventMeter`     — count of transition events on the board.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from .types import MeterReadings


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Meter(Protocol):
    """A single-axis counter. Implementations are thread-safe."""

    def value(self) -> int | timedelta:
        """Current reading. Integer for counters, ``timedelta`` for wall clock."""
        ...

    def reset(self) -> None:
        """Reset to zero / now. Called by the runtime at run start."""
        ...


class TickMeter:
    """Counts poll ticks. The runtime calls ``tick()`` once per iteration."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._count = 0

    def tick(self) -> None:
        with self._lock:
            self._count += 1

    def value(self) -> int:
        with self._lock:
            return self._count

    def reset(self) -> None:
        with self._lock:
            self._count = 0


class WallClockMeter:
    """Wall-clock elapsed since ``reset()``. Monotonic underneath."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._start = time.monotonic()

    def value(self) -> timedelta:
        with self._lock:
            return timedelta(seconds=time.monotonic() - self._start)

    def reset(self) -> None:
        with self._lock:
            self._start = time.monotonic()


class WorkerInvocationMeter:
    """Counts ``Chief.wake(trigger='worker')`` events.

    Subscribed to by the runtime via ``ChiefAgent.add_wake_listener``.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._count = 0

    def record(self, trigger: str) -> None:
        """Increment if the trigger matches a worker wake."""
        if trigger != "worker":
            return
        with self._lock:
            self._count += 1

    def value(self) -> int:
        with self._lock:
            return self._count

    def reset(self) -> None:
        with self._lock:
            self._count = 0


class LlmTokenMeter:
    """Accumulates LLM tokens reported by adapters / workers.

    Consumers call :meth:`report` with the token delta for each LLM call.
    The MAF adapter wires this up automatically; custom workers can call
    ``ctx.report_tokens(n)`` via the context passed by the runtime.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._count = 0

    def report(self, tokens: int) -> None:
        if tokens <= 0:
            return
        with self._lock:
            self._count += tokens

    def value(self) -> int:
        with self._lock:
            return self._count

    def reset(self) -> None:
        with self._lock:
            self._count = 0


class BoardEventMeter:
    """Counts board transition events since the run started.

    Subscribes to the ``QuadroBoard`` event stream via
    :meth:`QuadroBoard.add_event_listener`. Counts events with any of the
    frozen event types — task_posted, task_assigned, task_completed,
    task_reviewed, task_failed, task_stale, task_reassigned, task_heartbeat.

    ``filter`` may be passed to narrow the set; the default counts all.
    """

    def __init__(self, *, filter: Callable[[dict], bool] | None = None) -> None:  # noqa: A002
        self._lock = threading.Lock()
        self._count = 0
        self._filter = filter

    def record(self, event: dict) -> None:
        if self._filter is not None and not self._filter(event):
            return
        with self._lock:
            self._count += 1

    def value(self) -> int:
        with self._lock:
            return self._count

    def reset(self) -> None:
        with self._lock:
            self._count = 0


class MeterBundle:
    """Owns one of each concrete meter and produces :class:`MeterReadings` snapshots.

    Instantiated by the runtime at start and wired into the Chief (worker
    events), board (transition events), and LLM adapters (token reports).
    """

    def __init__(self) -> None:
        self.ticks = TickMeter()
        self.wall_clock = WallClockMeter()
        self.worker_invocations = WorkerInvocationMeter()
        self.llm_tokens = LlmTokenMeter()
        self.board_events = BoardEventMeter()

    def snapshot(self) -> MeterReadings:
        return MeterReadings(
            ticks=self.ticks.value(),
            wall_clock_elapsed=self.wall_clock.value(),
            worker_invocations=self.worker_invocations.value(),
            llm_tokens=self.llm_tokens.value(),
            board_events=self.board_events.value(),
        )

    def reset(self) -> None:
        self.ticks.reset()
        self.wall_clock.reset()
        self.worker_invocations.reset()
        self.llm_tokens.reset()
        self.board_events.reset()

    def report_llm_tokens(self, tokens: int) -> None:
        """Public hook for LLM adapters. Mirrors ``LlmTokenMeter.report``."""
        self.llm_tokens.report(tokens)

    # ── Subscribers the runtime attaches. ─────────────────────────────────────

    def on_chief_wake(self, trigger: str) -> None:
        self.worker_invocations.record(trigger)

    def on_board_event(self, event: Any) -> None:
        """Accept either a raw EventRecord-like object or a pre-serialised dict."""
        if hasattr(event, "to_dict"):
            self.board_events.record(event.to_dict())
        elif isinstance(event, dict):
            self.board_events.record(event)
        else:
            self.board_events.record({})

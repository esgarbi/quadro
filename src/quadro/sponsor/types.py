"""Core Sponsor/Lease types: protocol, value objects, decision union."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol, Union, runtime_checkable
from uuid import uuid4


def new_lease_id() -> str:
    """Mint a fresh lease id. 12 hex chars are enough for uniqueness within a run."""
    return uuid4().hex[:12]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Lease:
    """An issued promise that the runtime may keep working up to bounded amounts.

    Each axis is optional; ``None`` means "unbounded on this axis". At least
    one axis should be bounded in practice — an all-``None`` lease is valid but
    will never trigger renewal on its own (the Sponsor is only re-consulted
    on axis exhaustion). ``GoalSponsor`` uses such "unbounded" leases because
    it relies on predicate re-evaluation on exhaustion of a sibling's axis,
    typically via composition.

    Fields:
        id:
            Stable within this lease, unique per issuance. See
            ``renewal_of`` for the audit chain.
        issued_at:
            UTC datetime the lease was issued.
        ticks:
            Maximum number of poll ticks remaining. None = unbounded.
        deadline:
            Wall-clock deadline (UTC). None = unbounded.
        worker_invocations:
            Maximum cumulative worker invocations from now. None = unbounded.
        llm_tokens:
            Maximum cumulative LLM tokens from now. None = unbounded.
        board_events:
            Maximum cumulative board events from now. None = unbounded.
        source:
            Name of the Sponsor (or composer) that issued this lease. Used
            for telemetry and debugging.
        reason:
            Free-form reason attached by the issuing Sponsor.
        renewal_of:
            Id of the prior Lease this one replaces, or None for the first
            Lease of a run. Enables audit of the lease chain.
    """

    id: str = field(default_factory=new_lease_id)
    issued_at: datetime = field(default_factory=_utc_now)
    ticks: int | None = None
    deadline: datetime | None = None
    worker_invocations: int | None = None
    llm_tokens: int | None = None
    board_events: int | None = None
    source: str = "anonymous"
    reason: str = ""
    renewal_of: str | None = None

    def clamp(self) -> "Lease":
        """Return a lease with invalid axis values clamped to zero.

        Negative budgets or past deadlines are clamped to zero / now so that
        the next consultation point triggers an immediate renewal. This catches
        Sponsor bugs without crashing the runtime.
        """
        now = _utc_now()
        changes: dict[str, Any] = {}
        if self.ticks is not None and self.ticks < 0:
            changes["ticks"] = 0
        if self.worker_invocations is not None and self.worker_invocations < 0:
            changes["worker_invocations"] = 0
        if self.llm_tokens is not None and self.llm_tokens < 0:
            changes["llm_tokens"] = 0
        if self.board_events is not None and self.board_events < 0:
            changes["board_events"] = 0
        if self.deadline is not None and self.deadline < now:
            changes["deadline"] = now
        if not changes:
            return self
        return replace(self, **changes)

    def to_dict(self) -> dict[str, Any]:
        """Serialise for persistence on the board."""
        return {
            "id": self.id,
            "issued_at": self.issued_at.isoformat(),
            "ticks": self.ticks,
            "deadline": self.deadline.isoformat() if self.deadline else None,
            "worker_invocations": self.worker_invocations,
            "llm_tokens": self.llm_tokens,
            "board_events": self.board_events,
            "source": self.source,
            "reason": self.reason,
            "renewal_of": self.renewal_of,
        }


@dataclass(frozen=True)
class Continue:
    """Authorise the runtime to keep working under the bounds of ``lease``."""

    lease: Lease
    reason: str = ""


@dataclass(frozen=True)
class Drain:
    """Refuse new work assignments; allow in-flight tasks to finish.

    Once active work drops to zero or ``deadline`` passes, the runtime
    transitions to :class:`Stop`. A ``deadline`` of ``None`` defers to the
    runtime's configured ``drain_max_duration`` (default 5 minutes).
    """

    deadline: datetime | None
    reason: str = ""


@dataclass(frozen=True)
class Stop:
    """Terminate the runtime cleanly after the current poll iteration."""

    reason: str = ""


# The three-variant union every Sponsor returns. Type-narrowing uses
# ``isinstance`` on the dataclass types.
LeaseDecision = Union[Continue, Drain, Stop]


@dataclass(frozen=True)
class MeterReadings:
    """Absolute counters since the run started.

    Sponsors use these alongside the active lease to decide whether to renew,
    drain, or stop. For example, a ``LlmTokenBudgetSponsor`` compares
    ``meters.llm_tokens`` against its total budget.
    """

    ticks: int = 0
    wall_clock_elapsed: timedelta = timedelta()
    worker_invocations: int = 0
    llm_tokens: int = 0
    board_events: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticks": self.ticks,
            "wall_clock_elapsed_s": self.wall_clock_elapsed.total_seconds(),
            "worker_invocations": self.worker_invocations,
            "llm_tokens": self.llm_tokens,
            "board_events": self.board_events,
        }


@dataclass(frozen=True)
class SponsorContext:
    """Everything a Sponsor may read when proposing a lease.

    Fields:
        state:
            Full board state from ``bc.full_state()``. Tasks, agents, data.
        chief_telemetry:
            The ``_chief_telemetry`` dict as last persisted by the Chief. May
            be empty if the Chief has not run yet.
        meters:
            Absolute counters since the run started.
        lease_history:
            All leases issued in this run, oldest first. The last element is
            the currently-active lease at the time of consultation (also
            passed explicitly as ``prior`` to ``propose_lease``).
        now:
            Reference time, UTC.
    """

    state: dict
    chief_telemetry: dict
    meters: MeterReadings
    lease_history: tuple[Lease, ...]
    now: datetime


@runtime_checkable
class Sponsor(Protocol):
    """The authority that decides whether the runtime should keep working.

    Implementations return one of :class:`Continue`, :class:`Drain`, or
    :class:`Stop` in response to each consultation. Sponsors are consulted at
    startup, on lease axis exhaustion, and once more when drain completes.
    They are not consulted on every poll tick — batching is deliberate.

    Attributes:
        name: Short identifier used for telemetry. Optional; defaults to the
            class name when absent.
        fail_open: When True and the Sponsor raises, the runtime renews the
            previous lease instead of treating the exception as Stop. Default
            False (fail-closed). Implementations may expose this as an init
            parameter.
    """

    name: str

    def propose_lease(
        self, ctx: SponsorContext, prior: Lease | None
    ) -> LeaseDecision: ...

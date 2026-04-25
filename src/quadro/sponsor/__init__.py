"""Sponsor / Lease — lifetime and continuity for Quadro runtimes.

A Sponsor is the external authority that decides whether a Quadro runtime
should keep working. It is consulted by the runtime at well-defined moments
and returns one of three decisions: :class:`Continue`, :class:`Drain`, or
:class:`Stop`. Together with the :class:`Lease` value type, it replaces the
old ``done_when`` + ``max_cycles`` pair with a pluggable, multi-unit,
drain-aware lifetime model.

See ``docs/design/sponsor.md`` for the full design.
"""

from __future__ import annotations

from .composite import AllOf, AnyOf, Priority
from .fixtures import AlwaysOnSponsor, AlwaysStopSponsor, ScriptedSponsor
from .meters import (
    BoardEventMeter,
    Meter,
    MeterBundle,
    TickMeter,
    WallClockMeter,
    WorkerInvocationMeter,
)
from .sponsors import (
    BoardEventBudgetSponsor,
    CallableSponsor,
    CallbackSponsor,
    DeadlineSponsor,
    GoalSponsor,
    HttpSponsor,
    LlmTokenBudgetSponsor,
    QueueDepthSponsor,
    TickBudgetSponsor,
    WorkerBudgetSponsor,
)
from .types import (
    Continue,
    Drain,
    Lease,
    LeaseDecision,
    MeterReadings,
    Sponsor,
    SponsorContext,
    Stop,
    new_lease_id,
)

__all__ = [
    "AllOf",
    "AlwaysOnSponsor",
    "AlwaysStopSponsor",
    "AnyOf",
    "BoardEventBudgetSponsor",
    "BoardEventMeter",
    "CallableSponsor",
    "CallbackSponsor",
    "Continue",
    "DeadlineSponsor",
    "Drain",
    "GoalSponsor",
    "HttpSponsor",
    "Lease",
    "LeaseDecision",
    "LlmTokenBudgetSponsor",
    "Meter",
    "MeterBundle",
    "MeterReadings",
    "Priority",
    "QueueDepthSponsor",
    "ScriptedSponsor",
    "Sponsor",
    "SponsorContext",
    "Stop",
    "TickBudgetSponsor",
    "WallClockMeter",
    "WorkerInvocationMeter",
    "new_lease_id",
]

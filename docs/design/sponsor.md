# Sponsor / Lease — Lifetime and Continuity

Status: **Locked (v1)**. This document is the contract for all phases of the Sponsor/Lease implementation. Any change to names, API shape, or answers to the open questions below requires updating this document first.

## Problem statement

Quadro is a reactive framework: the Chief sleeps unless workers nudge it, and workers run only when the board has tasks for them to pick up. The runtime itself consumes almost nothing when nothing is happening.

In that model, the conventional agentic-framework idiom `max_cycles` is semantically weak:

- A "cycle" in the `RunLoop` is a poll tick (sleep + state read + done check + maybe ombudsman). It is not a Chief decision, not an LLM call, not a worker invocation. So capping it neither bounds cost nor tracks work.
- The `done_when` predicate answers *"has the mission been accomplished?"* That is a legitimate question, but it is not the same as *"should we still be working on this mission at all?"* Conflating the two hides the more interesting question: who decides to keep the system alive?

Quadro's differentiator, compared with "give an agent a prompt and hope", is that the framework asks the caller what *finished* looks like. The Sponsor/Lease layer extends that philosophy one level up: the framework also asks the caller what *commissioned* looks like.

## Concepts

### Sponsor

A Sponsor is the **external authority** that decides whether the runtime should keep working. It is consulted by the runtime at well-defined moments and returns one of three decisions: `Continue`, `Drain`, or `Stop`.

A Sponsor is not responsible for answering *"is the mission done?"* directly — the default `GoalSponsor` wraps a goal predicate for that purpose. A Sponsor can answer any question that bears on continuity: is the CRM ticket still open, is the batch window still active, is the token budget still positive, is there backlog left to process.

### Lease

A Lease is an issued promise that the runtime may keep working up to a bounded amount of work on any of several axes. If any axis is exhausted, the runtime must consult the Sponsor again for renewal.

Leases are renewable, not stateful across process restarts. Each `propose_lease` call issues a *new* Lease (with a fresh id) and optionally records `renewal_of` pointing at the previous lease for audit.

### LeaseDecision

Exactly one of:

- `Continue(lease, reason)` — authorise the runtime to keep working under the bounds of `lease`.
- `Drain(deadline, reason)` — no new work may be assigned; in-flight tasks may finish; once active work drops to zero (or `deadline` passes), the runtime transitions to `Stop`.
- `Stop(reason)` — terminate the runtime cleanly after the current poll iteration.

## Authoritative API

```python
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from uuid import uuid4


class Sponsor(Protocol):
    def propose_lease(
        self,
        ctx: "SponsorContext",
        prior: "Lease | None",
    ) -> "LeaseDecision": ...


@dataclass(frozen=True)
class Lease:
    id: str
    issued_at: datetime
    ticks: int | None = None
    deadline: datetime | None = None
    worker_invocations: int | None = None
    llm_tokens: int | None = None
    board_events: int | None = None
    source: str = "anonymous"
    reason: str = ""
    renewal_of: str | None = None


@dataclass(frozen=True)
class Continue:
    lease: Lease
    reason: str = ""


@dataclass(frozen=True)
class Drain:
    deadline: datetime | None
    reason: str = ""


@dataclass(frozen=True)
class Stop:
    reason: str = ""


LeaseDecision = Continue | Drain | Stop


@dataclass(frozen=True)
class MeterReadings:
    ticks: int = 0
    wall_clock_elapsed: timedelta = timedelta()
    worker_invocations: int = 0
    llm_tokens: int = 0
    board_events: int = 0


@dataclass(frozen=True)
class SponsorContext:
    state: dict                        # board snapshot from bc.full_state()
    chief_telemetry: dict              # _chief_telemetry dict, may be empty
    meters: MeterReadings              # absolute counters since run start
    lease_history: tuple["Lease", ...] # all leases issued in this run, oldest first
    now: datetime                      # reference time, UTC
```

Call site:

```python
runtime.sponsor(
    AllOf(
        GoalSponsor(lambda s: shipped(s) >= target),
        DeadlineSponsor.from_now(minutes=30),
        LlmTokenBudgetSponsor(100_000),
    )
).run(pipeline)
```

## Consultation points

The runtime consults the Sponsor at exactly three moments:

1. **Initial consultation** — before the first poll tick, to obtain the first Lease.
2. **Axis exhaustion** — whenever any lease axis has been met or exceeded (e.g. `ticks` counter reaches the lease's `ticks` limit, or `now >= deadline`).
3. **Drain-complete check** — while in drain, when no active tasks remain, the runtime asks the Sponsor one final time to confirm `Stop`. The Sponsor may return `Continue` with a fresh Lease to un-drain the system (rare but legal).

Sponsors are *not* consulted on every poll tick. The batching is deliberate: authority checks may be expensive (HTTP, CRM lookup) and the framework must not DoS a Sponsor's source of truth.

## Drain semantics

Drain is a first-class decision from day one.

- On `Drain(deadline, reason)`, the runtime sets an internal drain flag and signals the Chief to refuse new task assignments.
- The Chief continues to process worker completions, route `PENDING_REVIEW` and `REVISION_NEEDED` transitions, and allow in-flight tasks to reach a terminal state.
- Auto-Stop triggers when drain is active and no task is in a non-terminal, non-"pending" status (generalised across standard and custom profiles).
- If `deadline` is not `None` and `now >= deadline` before auto-Stop, the runtime force-stops regardless of in-flight work. A forced stop is still an orderly shutdown (shutdown hooks run).
- If `deadline` is `None`, the runtime uses the configured `drain_max_duration` fallback (default 5 minutes). A drain never runs forever.

Chief cooperation is implemented at two layers:

1. `ChiefAgent.set_draining(bool)` — flag on the Chief that `_apply_default_routing` respects (skips `UNASSIGNED -> IN_PROGRESS` and other dispatch transitions).
2. `dispatch_batch` in `src/quadro/dispatch.py` grows a drain-aware check so that even custom chief policies benefit without needing per-policy changes.

## Composition

Three composers are provided:

- `AllOf(*sponsors)` — every child must return a non-`Stop` decision. The effective lease is the axis-wise **minimum** of all children's leases. Any `Stop` short-circuits to `Stop`. Any `Drain` (with any `Continue`) short-circuits to `Drain`.
- `AnyOf(*sponsors)` — at least one child must return `Continue`. The effective lease is the axis-wise **maximum** of the continuing children's leases. `Stop` from all children is `Stop`; `Drain` from all children is `Drain`; a single `Continue` wins over siblings' `Drain` or `Stop`.
- `Priority(*sponsors)` — cascade in declaration order. First non-`Stop` wins outright. If all children return `Stop`, the composite returns `Stop`.

Mixed-decision truth table (for two children; larger composites fold pairwise):

| AllOf            | Continue | Drain | Stop |
|------------------|----------|-------|------|
| **Continue**     | Continue | Drain | Stop |
| **Drain**        | Drain    | Drain | Stop |
| **Stop**         | Stop     | Stop  | Stop |

| AnyOf            | Continue | Drain | Stop |
|------------------|----------|-------|------|
| **Continue**     | Continue | Continue | Continue |
| **Drain**        | Continue | Drain    | Drain    |
| **Stop**         | Continue | Drain    | Stop     |

## Built-in leaf Sponsors

| Name                        | Purpose                                                            |
|-----------------------------|--------------------------------------------------------------------|
| `GoalSponsor`               | Wraps a predicate over board state. Continue while false, Stop when true. Canonical replacement for `done_when`. |
| `DeadlineSponsor`           | Continue until wall-clock deadline. Has `.from_now(**td_kwargs)` classmethod. |
| `TickBudgetSponsor`         | Continue for N poll ticks. Parity replacement for the old `max_cycles=N`. |
| `WorkerBudgetSponsor`       | Continue while worker-invocations count below N.                   |
| `LlmTokenBudgetSponsor`     | Continue while LLM token consumption below N.                      |
| `BoardEventBudgetSponsor`   | Continue while board event count below N.                          |
| `CallableSponsor`           | Wraps a user callable returning a `LeaseDecision`.                 |
| `QueueDepthSponsor`         | Continue while a board data key has list length >= `min_depth`.    |

## External Sponsors

| Name               | Purpose                                                                  |
|--------------------|--------------------------------------------------------------------------|
| `HttpSponsor`      | Poll a remote endpoint for `{decision: ..., lease: ...}` JSON. Timeout, retry/backoff, configurable fail-mode (`fail_closed` default, `fail_open` opt-in). |
| `CallbackSponsor`  | Wrap an async callable for in-process integrations. Useful for Temporal workflows, orchestration engines. |

## Test fixtures

| Name                  | Purpose                                                                     |
|-----------------------|-----------------------------------------------------------------------------|
| `AlwaysOnSponsor`     | Always Continue with a Lease of configurable size. Primary test baseline.   |
| `AlwaysStopSponsor`   | Always Stop. Sanity-check termination paths.                                |
| `ScriptedSponsor`     | Deterministic sequence of decisions. Productionised as a supported replay/debug tool. |

## Error handling

- If `Sponsor.propose_lease` raises, the runtime treats the result as `Stop(reason="sponsor_error:<exc>")` by default (**fail-closed**).
- `fail_open=True` opt-in (per-Sponsor, not global) renews the previous lease on error. For `HttpSponsor`, also applies to network errors.
- Invalid lease values (negative ticks, past deadline, negative budgets) are clamped to zero, which triggers immediate renewal on the next consultation. This catches Sponsor bugs without crashing the runtime.
- No exception raised by a Sponsor can crash the `RunLoop`. There is an explicit test for this.

## Runtime integration

- `QuadroRuntime.sponsor(sponsor: Sponsor) -> QuadroRuntime` — fluent setter, required before `run()`.
- `QuadroRuntime.drain_max_duration(td: timedelta)` — optional override of default 5-minute drain fallback.
- `RunLoop` receives the Sponsor through its existing builder chain; a new `.sponsor(s)` method replaces `.done_when(...)` and `.max_cycles(...)` entirely.
- `poll_every`, `ombudsman_every`, `on_cycle`, `on_complete` are unchanged.

## Telemetry and observability

Two board data keys are mutated by the runtime during a Sponsor-governed run:

- `_sponsor_log` — list of decision records. Each record is `{sponsor_id, decision_type, reason, lease, meters, at}`. Bounded to the most recent N (default 200) to prevent unbounded growth.
- `_chief_telemetry` is extended with an `active_lease` sub-dict mirroring the current lease and `draining: bool`. The UI widget reads from here.

The UI widget shows:

- Current lease source (Sponsor name)
- Countdown on the nearest-exhausting axis
- Drain banner when active
- Last decision reason

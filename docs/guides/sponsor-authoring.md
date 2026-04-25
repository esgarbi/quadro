# Authoring a Sponsor

A Sponsor is the external authority that governs a Quadro runtime's
lifetime. Writing one is small and shaped like the rest of the framework:
a single method, returning a value.

## The protocol

```python
from quadro.sponsor import (
    Continue,
    Drain,
    Lease,
    LeaseDecision,
    SponsorContext,
    Stop,
)


class MySponsor:
    name = "my_sponsor"      # used for telemetry; required
    fail_open = False        # True = renew prior on exception; False = Stop

    def propose_lease(
        self,
        ctx: SponsorContext,
        prior: Lease | None,
    ) -> LeaseDecision:
        ...
```

Any object satisfying this shape is a Sponsor. You do **not** inherit from
a base class; the framework checks via the :class:`Sponsor` protocol.

## What you get to read

`SponsorContext` carries five fields:

| Field              | What it is                                                        |
|--------------------|-------------------------------------------------------------------|
| `state`            | Full board state (`tasks`, `agents`, `data`).                     |
| `chief_telemetry`  | Dict the Chief publishes each cycle (`draining`, `cycles_run`, ..). |
| `meters`           | Absolute counters: ticks, wall-clock, workers, tokens, events.    |
| `lease_history`    | Tuple of all leases issued in this run, oldest first.             |
| `now`              | Reference UTC datetime at consultation time.                      |

`prior` is the currently-active lease (or `None` on the first call).

## What you return

Exactly one of:

- `Continue(lease=Lease(...), reason="...")`
- `Drain(deadline=<datetime|None>, reason="...")`
- `Stop(reason="...")`

The `Lease` you pass in `Continue` expresses when the runtime should ask
you again. Each axis is optional — leave it `None` to mean "this axis is
unbounded". Common patterns:

- `Lease(ticks=ctx.meters.ticks + 1)` — re-consult after one more tick.
- `Lease(deadline=ctx.now + timedelta(seconds=30))` — re-consult in 30s.
- `Lease(llm_tokens=5_000)` — re-consult when cumulative tokens hit 5k.

You can set multiple axes; the runtime re-consults when any hits its
ceiling.

## Decision rules of thumb

- Use `Continue` when the source of truth says "keep going".
- Use `Drain` when the mission should wind down — new work should not be
  picked up, but in-flight work should finish. Drain is also the right
  answer when the queue has emptied and you want an orderly shutdown.
- Use `Stop` for hard halts: cancellation, budget exhausted, goal met.

## Use the built-ins when you can

Most real deployments can express their authority model as a composition
of existing Sponsors:

- `GoalSponsor(predicate)` replaces the old `done_when`.
- `DeadlineSponsor.from_now(minutes=30)` caps wall-clock time.
- `TickBudgetSponsor(N)` replaces the old `max_cycles=N`.
- `LlmTokenBudgetSponsor(N)` caps LLM spend.
- `QueueDepthSponsor(key, min_depth=1)` runs while backlog exists and
  drains otherwise.
- `HttpSponsor(url)` delegates to a remote authority.
- `CallableSponsor(fn)` wraps a plain Python callable.

Compose with `AllOf`, `AnyOf`, `Priority`. See the decision matrix.

## When to write a custom Sponsor

Write a custom Sponsor when:

- The source of truth is a proprietary system (internal CRM, database,
  message bus) that doesn't fit the HTTP protocol.
- Your authority requires stateful logic across consultations (e.g.
  "stop after three consecutive no-op cycles").
- You want to encode a lease-renewal heuristic — for instance, lease
  longer during quiet periods and shorter during busy ones.

## Error handling

`propose_lease` should not raise for normal cases. If it does raise, the
runtime catches it and treats the result as `Stop` (default). Set
`fail_open=True` on your Sponsor to have the runtime renew the previous
lease instead of stopping. Fail-open is appropriate when the Sponsor
depends on a potentially flaky external system and you would rather
continue on last-known-good than stop.

Always prefer returning an explicit `Stop(reason=...)` over raising — the
reason string is recorded in the `_sponsor_log` and surfaces in the UI.

## Testing your Sponsor

Use :class:`ScriptedSponsor` to simulate interactions in tests, or drive
your Sponsor directly with hand-built `SponsorContext` objects:

```python
from datetime import datetime, timezone
from quadro.sponsor import MeterReadings, SponsorContext

ctx = SponsorContext(
    state={"tasks": [], "agents": [], "data": {}},
    chief_telemetry={},
    meters=MeterReadings(),
    lease_history=(),
    now=datetime.now(timezone.utc),
)
decision = my_sponsor.propose_lease(ctx, prior=None)
```

## Persistence

Leases are in-memory. A process restart begins a fresh consultation chain.
If your Sponsor needs to remember anything across restarts (e.g. "we
already processed ticket X"), encode it in your source of truth — the CRM
ticket's own status, a database row, an external audit log. Do not encode
it in the Lease.

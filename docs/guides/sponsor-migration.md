# Migrating from `done_when` / `max_cycles` to Sponsor

In v0.2 the `done_when` predicate and `max_cycles` safety net are removed
from `QuadroRuntime`, `RunLoop`, and `BuiltPipeline.run`. They are replaced
by a single seam — `.sponsor(...)` — that accepts any object satisfying
the [`Sponsor` protocol](../design/sponsor.md). All existing semantics are
recoverable by composing the built-in leaf sponsors.

This guide covers the three migrations every downstream caller needs:
`done_when`, `max_cycles`, and `BuiltPipeline.run`. The `RunLoop` builder
chain (`poll_every`, `ombudsman_every`, `on_cycle`, `on_complete`) is
unchanged.

For the full design and locked API, see [`../design/sponsor.md`](../design/sponsor.md).
For an "I want X, use Y" lookup of the built-in sponsors, see
[`sponsor-decision-matrix.md`](sponsor-decision-matrix.md).

---

## 1. Goal-based termination — `done_when` → `GoalSponsor`

`GoalSponsor(predicate)` is the canonical drop-in for `.done_when(predicate)`.
It re-evaluates the predicate at each Sponsor consultation and returns
`Stop` once the predicate is true.

**Before**

```python
runtime.done_when(
    lambda state: all(t["status"] == "COMPLETE" for t in state["tasks"])
).run(pipeline)
```

**After**

```python
from quadro.sponsor import GoalSponsor

runtime.sponsor(
    GoalSponsor(
        lambda state: all(t["status"] == "COMPLETE" for t in state["tasks"])
    )
).run(pipeline)
```

The predicate signature and contract are identical. `GoalSponsor` reads
`SponsorContext.state` (the board snapshot) and applies your predicate.

---

## 2. Cycle cap — `max_cycles` → `TickBudgetSponsor`

`TickBudgetSponsor(n)` is the parity replacement: it issues a Lease that
expires after `n` poll ticks, at which point the runtime stops.

**Before**

```python
runtime.done_when(predicate).max_cycles(500).run(pipeline)
```

**After (cap as belt-and-braces alongside a goal)**

```python
from quadro.sponsor import AllOf, GoalSponsor, TickBudgetSponsor

runtime.sponsor(
    AllOf(
        GoalSponsor(predicate),
        TickBudgetSponsor(500),
    )
).run(pipeline)
```

`AllOf` short-circuits to `Stop` if any child Sponsor returns `Stop`.
The first to terminate wins — the goal-met path or the tick-cap path,
whichever comes first.

**After (drop the cap entirely if the goal is sufficient)**

If `max_cycles` was only a defensive hedge and you trust the goal
predicate to terminate, you can simplify to a bare `GoalSponsor`. This is
the recommended shape for new code.

```python
runtime.sponsor(GoalSponsor(predicate)).run(pipeline)
```

---

## 3. Built pipeline — `BuiltPipeline.run(done_when=, max_cycles=)` → `runtime.run(pipeline)`

`BuiltPipeline.run(...)` is removed in v0.2. The single runnable entry
point is now `QuadroRuntime.run(pipeline)`. This keeps one code path
through the system and one migration target for downstream callers.

**Before**

```python
pipeline = build_pipeline(...)
final = pipeline.run(
    done_when=lambda s: shipped(s) >= target,
    max_cycles=1000,
)
```

**After**

```python
from quadro.sponsor import AllOf, GoalSponsor, TickBudgetSponsor

pipeline = build_pipeline(...)
runtime = QuadroRuntime(backend).with_profiles(...)
final = (
    runtime
    .sponsor(
        AllOf(
            GoalSponsor(lambda s: shipped(s) >= target),
            TickBudgetSponsor(1000),
        )
    )
    .run(pipeline)
)
```

The `examples/microsoft_agent_framework/*/main_pipeline.py` files
demonstrate this shape against the LLM-backed examples.

---

## 4. Adding richer authorities (new in v0.2)

The point of moving to a Sponsor seam is that you can now express
authorities that the old `done_when` / `max_cycles` pair could not.

**Wall-clock deadline** — stop after a fixed duration regardless of
progress:

```python
from quadro.sponsor import DeadlineSponsor

runtime.sponsor(DeadlineSponsor.from_now(minutes=30)).run(pipeline)
```

**Cost ceiling** — stop when LLM token consumption exceeds a budget. Wire
your LLM adapter's token reporter into `runtime.meters.report_llm_tokens`
so the meter sees real usage:

```python
from quadro.sponsor import LlmTokenBudgetSponsor

runtime.sponsor(LlmTokenBudgetSponsor(100_000)).run(pipeline)
```

**External authority (HTTP)** — let an upstream system decide whether the
runtime should still be working. The endpoint receives a small JSON
envelope and returns a Sponsor decision:

```python
from quadro.sponsor import HttpSponsor

runtime.sponsor(
    HttpSponsor(url="https://control.example.com/sponsor")
).run(pipeline)
```

`HttpSponsor` defaults to fail-closed — a network error returns `Stop`.
Use `fail_open=True` to renew the previous Lease on transient failure.

**Composition** — combine authorities; `AllOf` requires every child to
authorise continuation, `AnyOf` lets any single child authorise it:

```python
from quadro.sponsor import (
    AllOf, DeadlineSponsor, GoalSponsor, LlmTokenBudgetSponsor
)

runtime.sponsor(
    AllOf(
        GoalSponsor(lambda s: shipped(s) >= target),
        DeadlineSponsor.from_now(minutes=30),
        LlmTokenBudgetSponsor(100_000),
    )
).run(pipeline)
```

---

## 5. Drain — first-class graceful shutdown (new in v0.2)

A Sponsor may return `Drain(deadline, reason)` instead of `Stop`. Drain
suppresses **new** task assignment while letting in-flight work reach a
terminal state. The runtime auto-stops when active work hits zero, or
when the drain deadline passes (whichever is first).

`Drain(deadline=None)` falls back to the runtime's
`drain_max_duration` (default 5 minutes). Override per-runtime if needed:

```python
from datetime import timedelta

runtime.drain_max_duration(timedelta(minutes=30))
```

There was no equivalent in the `done_when` / `max_cycles` model. Existing
callers do not need to do anything to opt in to drain — it is
transparently available the moment you install a Sponsor that can return
`Drain`. See `examples/core/crm_sponsor/` for a worked demonstration.

---

## 6. Test fixtures (new in v0.2)

Three Sponsor implementations are intended for tests:

- `AlwaysOnSponsor(lease_size=...)` — always `Continue`. The primary
  baseline for tests that don't care about lifetime.
- `AlwaysStopSponsor()` — always `Stop`. Sanity-check termination paths.
- `ScriptedSponsor([decision1, decision2, ...])` — deterministic
  sequence of decisions. Productionised as a supported replay/debug tool;
  use it to reproduce a specific Sponsor decision sequence in a regression
  test.

```python
from quadro.sponsor import AlwaysOnSponsor

runtime.sponsor(AlwaysOnSponsor()).run(pipeline)
```

---

## Common questions

**Q: My `done_when` was reading both task state and external state.
Does `GoalSponsor` cover that?**

`GoalSponsor` only sees `SponsorContext.state` (the board snapshot). For
external state, use `CallableSponsor` and read the external source
inside your callable, or use `HttpSponsor` and let the external authority
return the decision directly.

**Q: I had `max_cycles` set to a very high number as a "safety net I
hope I never hit". Do I still need it?**

If your goal predicate is sound, no — `GoalSponsor` alone is sufficient.
If you want defence in depth (e.g. against a bug in the goal predicate),
combine with `TickBudgetSponsor` or `DeadlineSponsor` via `AllOf`. The
Sponsor layer makes the safety net explicit rather than implicit.

**Q: My Sponsor needs to remember state across calls.**

Sponsors are objects; you can hold state on `self`. For state that needs
to survive process restarts, encode it in your authority's source of
truth (a CRM ticket, a database row, an HTTP endpoint) — that is the
intent behind making Sponsors external.

**Q: Can I write a Sponsor that just runs forever?**

`AlwaysOnSponsor` does this for tests. In production, you should always
have at least one terminating axis — even an unrealistically large
`DeadlineSponsor` is preferable to none, because it bounds the failure
mode of a stuck Sponsor.

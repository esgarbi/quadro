# Sponsor decision matrix

Pick the Sponsor (or combination) that matches your authority model.

## "I want to run until X is done."

Classic goal. Use :class:`GoalSponsor`.

```python
from quadro.sponsor import GoalSponsor

runtime.sponsor(GoalSponsor(lambda state: shipped(state) >= 10))
```

This is the drop-in replacement for the old `done_when(predicate)`.

## "I want a safety cap so the run can't go forever."

Compose with `AllOf(..., TickBudgetSponsor(N))`.

```python
from quadro.sponsor import AllOf, GoalSponsor, TickBudgetSponsor

runtime.sponsor(AllOf(
    GoalSponsor(predicate),
    TickBudgetSponsor(1000),         # parity replacement for max_cycles
))
```

## "I want a wall-clock deadline."

Add `DeadlineSponsor`.

```python
from quadro.sponsor import AllOf, DeadlineSponsor, GoalSponsor

runtime.sponsor(AllOf(
    GoalSponsor(predicate),
    DeadlineSponsor.from_now(minutes=30),
))
```

## "I want to bound LLM cost."

Add `LlmTokenBudgetSponsor`. Custom workers should call
`ctx.report_tokens(n)` on every LLM call; MAF-backed workers are auto-metered.

```python
from quadro.sponsor import AllOf, GoalSponsor, LlmTokenBudgetSponsor

runtime.sponsor(AllOf(GoalSponsor(pred), LlmTokenBudgetSponsor(100_000)))
```

## "I want to keep working while there's backlog, then wind down."

Use `QueueDepthSponsor`. By default it returns `Drain` when the queue is
empty, so in-flight work finishes before the runtime stops.

```python
from quadro.sponsor import QueueDepthSponsor

runtime.sponsor(QueueDepthSponsor("orders_in_queue", min_depth=1))
```

## "An external system owns 'should we keep running'."

Use `HttpSponsor` for an HTTP API, `CallbackSponsor` for an in-process
async callable, or `CallableSponsor` for a sync lambda.

```python
from quadro.sponsor import AllOf, DeadlineSponsor, HttpSponsor

runtime.sponsor(AllOf(
    HttpSponsor("https://crm.example.com/sponsor/ticket-123",
                timeout=5.0, max_retries=2, fail_open=False),
    DeadlineSponsor.from_now(hours=8),     # belt-and-braces
))
```

## "I want a primary authority with a fallback."

Use `Priority`: the first non-`Stop` child wins.

```python
from quadro.sponsor import DeadlineSponsor, HttpSponsor, Priority

runtime.sponsor(Priority(
    HttpSponsor("https://crm.example.com/sponsor/ticket-123"),
    DeadlineSponsor.from_now(hours=1),  # consulted only if CRM says Stop
))
```

## "Multiple authorities — any of them approving is enough."

Use `AnyOf`.

```python
from quadro.sponsor import AnyOf, QueueDepthSponsor

runtime.sponsor(AnyOf(
    QueueDepthSponsor("orders_in_queue"),
    QueueDepthSponsor("priority_queue", min_depth=1),
))
```

## Composition rules at a glance

| Composer   | Input                    | Output                                  |
|------------|--------------------------|-----------------------------------------|
| `AllOf`    | Continue + Continue      | Continue, lease is axis-wise **min**    |
|            | Continue + Drain         | Drain (min deadline)                    |
|            | any + Stop               | Stop                                    |
| `AnyOf`    | Continue + anything      | Continue (lease is axis-wise **max**)   |
|            | Drain + Drain            | Drain (max deadline)                    |
|            | Stop + Drain             | Drain                                   |
|            | Stop + Stop              | Stop                                    |
| `Priority` | First non-Stop wins      | That decision (with source=priority)    |

## Testing

Use `ScriptedSponsor` for deterministic integration tests:

```python
from quadro.sponsor import Continue, Drain, Lease, ScriptedSponsor, Stop

sponsor = ScriptedSponsor([
    Continue(lease=Lease(ticks=2)),
    Continue(lease=Lease(ticks=2)),
    Drain(deadline=None, reason="test_drain"),
    Stop(reason="test_stop"),
])
```

Exhausting the script returns `Stop(reason="script_exhausted")` by default.

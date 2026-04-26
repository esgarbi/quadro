# CRM-gated Quadro runtime

This example is the headline illustration of Quadro's **continuity story**.

Instead of a predicate hard-coded into the runtime ("run until N tasks are
done"), the lifetime of the run is delegated to an external authority: a
mocked CRM ticket. As long as the ticket is `open`, the runtime keeps
working. When the ticket flips to `in_review`, the runtime **drains**:
no new tasks are assigned, but in-flight work finishes cleanly. When the
ticket becomes `closed`, the runtime stops.

Swap `Crm` for a real CRM client (HTTP, database, message bus) and the
shape of the Sponsor — a function that maps ticket state to
`Continue / Drain / Stop` — does not change.

## What to look for when you run it

```bash
python examples/crm_sponsor/main.py
```

- The run starts with five tasks on the board and the ticket in `open`.
  Workers sleep briefly per task so you can actually *see* work in flight
  rather than having it all complete inside the seed nudge.
- The CRM schedule is armed on the first loop cycle (not at setup time),
  so the transitions play out relative to the loop — robust to slow startup,
  debuggers, and any environment where the first few milliseconds are
  unpredictable.
- ~1s in, the ticket evolves to `in_review` and the Sponsor returns
  `Drain`. The RunLoop publishes the drain flag to the board; the chief
  stops picking up new UNASSIGNED tasks. Workers that were already in
  flight continue to their natural terminal state.
- ~3s in, the ticket *would* evolve to `closed` and the Sponsor *would*
  return `Stop`. In practice drain completes first (all five tasks finish
  well before 3s) and the runtime exits via the drain-complete path, so
  the `closed` transition often does not fire before the run ends. That
  is the designed behaviour — the Sponsor acknowledges that drain-complete
  beats an external close signal.
- The final summary prints the tasks, the sequence of sponsor decisions
  (one per lease expiry or state change — expect several `continue`s and
  one `drain`), and the ticket's closing status at the moment the run
  ended.

## Replacing the mock CRM with a real one

`make_crm_sponsor` wraps the CRM lookup in a `CallableSponsor`. For an
HTTP-backed CRM you can use `HttpSponsor` directly — it speaks a tiny JSON
protocol:

```
POST /sponsor
{"now": "...", "prior_lease_id": "...", "meters": {...}, "state_summary": {...}}

Response 200
{"decision": "continue" | "drain" | "stop",
 "reason": "...",
 "lease": {"ticks": 3, "deadline": "ISO8601", ...}}
```

See `src/quadro/sponsor/sponsors.py` for `HttpSponsor`'s implementation.

## Why this is different from a `done_when`

`done_when` answered "has the mission been accomplished?". That is still a
legitimate question — `GoalSponsor(predicate)` is the canonical drop-in.

But the *authority* question — should we still be working on this mission?
— was hidden behind `max_cycles`. A CRM ticket is a much better answer:
it is the real source of truth about whether the work should continue, it
can be cancelled, it can request a drain, it already encodes your
organisation's approval process. Delegating the runtime's lifetime to it
means Quadro doesn't have to pretend it knows when to stop.

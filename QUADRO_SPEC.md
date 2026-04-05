# Quadro Specification — v0.1

This document is the normative specification for the Quadro coordination layer.
The reference implementation in `src/quadro/` is the authoritative realisation of
this spec. Where implementation and spec diverge, the spec describes the intended
behaviour and the implementation should be corrected.

Read `README.md` for motivation and architecture overview. Read
`IMPLEMENTATION_ROADMAP.md` for milestone status. This document specifies the
contracts.

---

## 1. Concepts

### 1.1 The Board

The Board is the single durable surface for all coordination state. It holds tasks,
agent registrations, and an immutable event log. All components — Chief, Workers,
Ombudsman — interact with the system exclusively through the Board's A2A interface.
No component holds private coordination state between invocations.

### 1.2 Task

A Task is the unit of work. It has a type, a label, a priority, a lifecycle profile,
a status, an optional assignment, and an optional output. Tasks are created by
callers via `board.post_task` and transition through states defined by their lifecycle
profile. A task's full history is reconstructable from the event log at any time.

### 1.3 Agent

An Agent is a registered worker or coordinator. Agents are registered via
`board.register_agent` with an AgentCard declaring their identity, capabilities, and
A2A URL. The Board tracks each agent's status (`IDLE`, `BUSY`, `OFFLINE`) and their
current task assignment. Agent status is updated automatically as tasks transition.

### 1.4 Lifecycle Profile

A Lifecycle Profile is a formal contract for a class of work. It is a set of valid
`(from_status, to_status)` transition pairs declared at startup. The Board rejects
any transition not present in the profile with a `TransitionError` before any
application code runs. Every profile is automatically expanded with global exits
(`HUMAN_REVIEW`, `ON_HOLD`) from all states.

Two standard profiles are built in:

- `review_required` — tasks require explicit review and approval before completion
- `fast` — tasks move directly from work to completion without review

Custom profiles are declared as sets of string pairs and resolved by task type via
a `profile_resolver` mapping supplied at board construction time.

### 1.5 Frozen Taxonomy

The set of valid event types is fixed and immutable. No application code may emit
an event type outside this set. The frozen taxonomy is:

```
task_posted
task_assigned
task_heartbeat
task_completed
task_reviewed
task_stale
task_reassigned
task_failed
```

`task_heartbeat` is an operational signal stored in the event log but excluded from
Chief wakeup triggers. All other event types trigger Chief wakeup.

### 1.6 The Chief

The Chief is the coordinator. It never executes tasks. It reads the Board, decides
what should happen next, writes those decisions back, dispatches workers, and sleeps.
Only one Chief decision cycle runs at a time — concurrent wakeup signals are
serialised and coalesced into a single board read.

### 1.7 Hydration

Before a worker executes, the system assembles its full working context from the
Board's current state. The resulting context is deterministic: same Board state
produces the same context. A `context_snapshot_hash` is stored on the task record
to make this verifiable.

### 1.8 Reactive Wakeup

The Chief is woken by a signal that carries no payload — only the fact that the Board
changed. The Chief always reads the full board on wake, never a partial event stream.
This ensures the Chief always reasons from a consistent snapshot, regardless of how
many events occurred between cycles.

### 1.9 The Ombudsman

The Ombudsman monitors in-progress tasks for stale heartbeats. When a task's
`heartbeat_at` is absent or older than the configured timeout, the Ombudsman
transitions it to `STALE` and wakes the Chief. The Chief's normal routing then
transitions `STALE → UNASSIGNED` for reassignment. Recovery is not a special case.

---

## 2. Data Records

### 2.1 TaskRecord

| Field | Type | Description |
|---|---|---|
| `task_id` | `str` | Unique identifier. Generated as `uuid4().hex[:5]` if not supplied. |
| `task_type` | `str` | Determines the lifecycle profile via `profile_resolver`. |
| `label` | `str` | Human-readable description of the task. |
| `priority` | `int` | Dispatch priority. Default `5`. Lower values = higher priority (convention). |
| `status` | `TaskStatus \| str` | Current lifecycle state. Initial value: `UNASSIGNED`. |
| `assigned_to` | `str \| None` | `agent_id` of the currently responsible agent. |
| `output` | `str \| None` | Result written by the worker on completion. |
| `notes` | `list[str]` | Append-only notes. Workers may append; existing entries are immutable. |
| `continuation_token` | `str \| None` | Reserved for long-running worker resumption. |
| `heartbeat_at` | `datetime \| None` | UTC timestamp of last heartbeat. `None` if no heartbeat posted. |
| `context_snapshot_hash` | `str \| None` | Hash of hydrated context at dispatch time. |
| `created_at` | `datetime` | UTC timestamp of task creation. Immutable after creation. |
| `updated_at` | `datetime` | UTC timestamp of last mutation. |

### 2.2 AgentRecord

| Field | Type | Description |
|---|---|---|
| `agent_id` | `str` | Unique identifier. |
| `name` | `str` | Human-readable name. |
| `status` | `AgentStatus` | `IDLE`, `BUSY`, or `OFFLINE`. |
| `capabilities` | `list[str]` | Task types this agent can handle. |
| `a2a_url` | `str` | A2A endpoint for dispatch. |
| `agent_card` | `dict` | Full registration payload. |
| `current_task_id` | `str \| None` | Task currently assigned to this agent. |
| `version` | `str \| None` | Agent version string. |
| `last_seen_at` | `datetime` | UTC timestamp of last activity. |

### 2.3 EventRecord

| Field | Type | Description |
|---|---|---|
| `sequence_id` | `int` | Monotonically increasing. Assigned by the backend on append. |
| `event_type` | `str` | One of the frozen taxonomy values. |
| `task_id` | `str` | Task this event pertains to. |
| `agent_id` | `str \| None` | Agent responsible for the transition, if any. |
| `from_status` | `TaskStatus \| str \| None` | Status before transition. `None` for `task_posted`. |
| `to_status` | `TaskStatus \| str \| None` | Status after transition. |
| `payload` | `dict` | Intent-specific metadata (e.g. `{"profile": "review_required"}`). |
| `idempotency_key` | `str \| None` | Caller-supplied key for deduplication (stored; not yet enforced — see TODO item 1). |
| `timestamp` | `datetime` | UTC timestamp of event creation. |

Events are immutable once written. The event log is append-only. No event may be
deleted, modified, or re-ordered.

---

## 3. Status Enumerations

### 3.1 TaskStatus

Standard statuses used by the built-in lifecycle profiles:

| Value | Meaning |
|---|---|
| `UNASSIGNED` | Task posted, not yet assigned to a worker. Initial state. |
| `IN_PROGRESS` | Assigned to a worker and actively being executed. |
| `PENDING_REVIEW` | Work complete; awaiting review agent dispatch. |
| `REVISION_NEEDED` | Reviewer rejected the output; task returned to the worker pool. |
| `APPROVED` | Reviewer accepted the output; awaiting final completion. |
| `COMPLETE` | Terminal. Work is done and accepted. |
| `STALE` | Heartbeat timeout exceeded. Awaiting reassignment. |
| `HUMAN_REVIEW` | Global exit. Task requires human intervention. |
| `ON_HOLD` | Global exit. Task suspended indefinitely. |

Custom profiles may use arbitrary string statuses in place of the standard values,
with the exception of `HUMAN_REVIEW` and `ON_HOLD` which are always valid exits.

### 3.2 AgentStatus

| Value | Meaning |
|---|---|
| `IDLE` | Available for dispatch. |
| `BUSY` | Assigned to a task and executing. |
| `OFFLINE` | Not available (reserved; not yet enforced). |

---

## 4. Lifecycle Profiles

### 4.1 `review_required`

```
UNASSIGNED    → IN_PROGRESS
IN_PROGRESS   → PENDING_REVIEW
IN_PROGRESS   → APPROVED
IN_PROGRESS   → REVISION_NEEDED
PENDING_REVIEW → IN_PROGRESS
REVISION_NEEDED → IN_PROGRESS
APPROVED      → COMPLETE
IN_PROGRESS   → STALE
STALE         → UNASSIGNED
+ global exits from all states → HUMAN_REVIEW, ON_HOLD
```

### 4.2 `fast`

```
UNASSIGNED  → IN_PROGRESS
IN_PROGRESS → COMPLETE
IN_PROGRESS → STALE
STALE       → UNASSIGNED
+ global exits from all states → HUMAN_REVIEW, ON_HOLD
```

### 4.3 Custom profiles

Declared as `set[tuple[str, str]]` and passed to `QuadroBoard` at construction via
`custom_profiles={"profile_name": transition_set}`. The `profile_resolver` dict maps
`task_type` strings to profile names.

Custom profiles are automatically expanded with `HUMAN_REVIEW` and `ON_HOLD` exits
from every state. Terminal statuses are derived mechanically as states that appear
only as destinations — never as sources — in the declared transition set, excluding
the auto-expanded exits.

---

## 5. A2A Interface

All inter-component communication uses A2A envelopes over a registered transport.
The reference implementation uses `LocalA2ANetwork` (in-process). HTTP transport
is planned (TODO item 6).

### 5.1 Request Envelope

```json
{
  "intent":           "board.post_task",
  "request_id":       "a1b2c",
  "idempotency_key":  "optional-caller-key",
  "timestamp":        "2025-03-30T14:00:00+00:00",
  "payload":          {}
}
```

All fields except `idempotency_key` are required. `request_id` is generated by the
caller if not supplied. The `intent` must be one of the values in `ALLOWED_INTENTS`.

### 5.2 Response Envelope

```json
{
  "request_id": "a1b2c",
  "ok":         true,
  "result":     {},
  "error":      null
}
```

On failure, `ok` is `false`, `error` contains a string description, and `result` is
an empty dict. Application code must check `ok` before using `result`.

### 5.3 Allowed Intents

```
board.post_task
board.update_task
board.get_task
board.get_full_state
board.register_agent
board.post_agent_heartbeat
board.stream_events
board.put_data
board.get_data
board.get_task_history
board.get_agent_activity
worker.post_result
worker.execute_task
worker.request_help
chief.apply_actions
chief.wake
```

---

## 6. Board Intents

### 6.1 `board.post_task`

Creates a new task in `UNASSIGNED` status and emits `task_posted`.

**Request payload:**

| Field | Required | Description |
|---|---|---|
| `task_type` | Yes | Determines lifecycle profile. |
| `label` | Yes | Human-readable description. |
| `task_id` | No | Caller-supplied ID. Generated if absent. |
| `priority` | No | Integer. Default `5`. |
| `notes` | No | Initial notes list. Default `[]`. |

**Response result:** `{"task": TaskRecord, "event": EventRecord}`

---

### 6.2 `board.update_task`

Transitions a task to a new status. Validates the transition against the task's
lifecycle profile. Emits the appropriate frozen event type. Updates agent status
if `assigned_to` is set.

**Request payload:**

| Field | Required | Description |
|---|---|---|
| `task_id` | Yes | Task to update. |
| `to_status` | Yes | Target status. Must be a valid transition from current status. |
| `assigned_to` | No | Agent taking responsibility. |
| `output` | No | Result data. Stored as string. |
| `label` | No | Updated label. |
| `notes_append` | No | Single string appended to `notes`. |
| `context_snapshot_hash` | No | Hash of hydrated context at dispatch. |

**Raises:** `TransitionError` if the transition is not valid for the task's profile.

**Response result:** `{"task": TaskRecord, "event": EventRecord}`

---

### 6.3 `board.get_task`

Returns a single task by ID.

**Request payload:** `{"task_id": str}`

**Response result:** `{"task": TaskRecord}`

**Raises:** `KeyError` if task not found.

---

### 6.4 `board.get_full_state`

Returns the complete board state: all tasks, all agents, and all data entries.
Used by the Chief on every wakeup cycle.

**Request payload:** `{}`

**Response result:** `{"tasks": list[TaskRecord], "agents": list[AgentRecord], "data": dict}`

---

### 6.5 `board.register_agent`

Registers or updates an agent's AgentCard. Idempotent — re-registering the same
`agent_id` updates the existing record.

**Required AgentCard fields:** `agent_id`, `name`, `url`, `version`, `capabilities`, `description`

**Response result:** `{"agent": AgentRecord}`

---

### 6.6 `board.post_agent_heartbeat`

Updates an agent's `last_seen_at` and, if `task_id` is supplied, updates the task's
`heartbeat_at`. Emits `task_heartbeat` (operational; does not wake the Chief).

**Request payload:** `{"agent_id": str, "task_id": str | null}`

**Response result:** `{"agent": AgentRecord, "task": TaskRecord | null, "event": EventRecord | null}`

---

### 6.7 `board.stream_events`

Returns all events with `sequence_id > since_sequence`, ordered ascending.

**Request payload:** `{"since_sequence": int}`

**Response result:** `{"events": list[EventRecord]}`

---

### 6.8 `board.put_data` / `board.get_data`

Arbitrary key-value store on the Board. Used for shared pipeline data (inventory,
configuration, flight log entries). Data writes do not emit events and do not
trigger Chief wakeup.

**`board.put_data` payload:** `{"key": str, "value": dict}`

**`board.get_data` payload:** `{"key": str}`

**`board.get_data` result:** `{"key": str, "value": dict | null}`

---

### 6.9 `board.get_task_history`

Returns all events for a specific task, ordered by `sequence_id` ascending.
Unknown task IDs return an empty list, not an error.

**Request payload:** `{"task_id": str}`

**Response result:** `{"task_id": str, "events": list[EventRecord]}`

---

### 6.10 `board.get_agent_activity`

Returns all events involving a specific agent, ordered by `sequence_id` ascending.
Unknown agent IDs return an empty list, not an error.

**Request payload:** `{"agent_id": str}`

**Response result:** `{"agent_id": str, "events": list[EventRecord]}`

---

## 7. Worker Intents

### 7.1 `worker.execute_task`

Dispatched by the Chief to a Worker's registered A2A URL. Carries the `task_id`.
The worker reads its full context from the Board via `board.get_full_state` or
`board.get_task` (Hydration), executes, and writes results back to the Board.

**Payload:** `{"task_id": str}`

---

### 7.2 `worker.post_result`

Convenience intent allowing a worker to post its result and trigger the correct
status transition in a single call. The Board determines the correct target status
from the task's lifecycle profile:

- `review_required` → transitions to `PENDING_REVIEW`
- `fast` → transitions to `COMPLETE`

Emits `task_completed`. Updates agent status to `IDLE`.

**Request payload:**

| Field | Required | Description |
|---|---|---|
| `task_id` | Yes | Task being completed. |
| `output` | Yes | Result data. |
| `agent_id` | No | Agent posting the result. |

**Raises:** `TransitionError` if task is not `IN_PROGRESS`.

---

## 8. Event Type → Transition Mapping

The Board derives the correct event type for each transition automatically. The
mapping is:

| Transition | Event type |
|---|---|
| `None → UNASSIGNED` | `task_posted` |
| `UNASSIGNED → IN_PROGRESS` | `task_assigned` |
| `PENDING_REVIEW → IN_PROGRESS` | `task_assigned` |
| `REVISION_NEEDED → IN_PROGRESS` | `task_assigned` |
| `STALE → UNASSIGNED` | `task_reassigned` |
| `* → STALE` | `task_stale` |
| `* → HUMAN_REVIEW` | `task_failed` |
| `IN_PROGRESS → PENDING_REVIEW` | `task_completed` |
| `IN_PROGRESS → COMPLETE` | `task_completed` |
| `IN_PROGRESS → APPROVED` | `task_reviewed` |
| `IN_PROGRESS → REVISION_NEEDED` | `task_reviewed` |
| `APPROVED → COMPLETE` | `task_reviewed` |
| Custom `* → *` | `task_completed` |
| `* → *` (heartbeat) | `task_heartbeat` |

---

## 9. Architecture Invariants

These invariants must hold in all conforming implementations. Tests must verify each
one. No future change may violate them without a versioning decision.

1. **Board is the single source of truth.** All coordination state is persisted on
   the Board. No component holds state between invocations that affects routing,
   dispatch, or lifecycle decisions.

2. **A2A-only boundaries.** No direct method calls between Board, Chief, and Workers.
   All cross-component communication goes through registered A2A endpoints. The
   reference implementation uses `network.request()` for all such calls.

3. **Single transition, single event.** Every valid state transition emits exactly
   one immutable event. Invalid transitions emit nothing and persist nothing.

4. **Chief serialisation.** Only one Chief decision cycle runs at a time. Concurrent
   wakeup signals are serialised. The second caller sees a `_pending_wake` flag set
   and returns without running a cycle; the Chief runs a follow-up cycle after the
   first completes.

5. **Frozen event taxonomy.** Only the eight event types in `FROZEN_EVENT_TYPES` are
   valid. Adding a new event type is a versioning decision requiring a spec update.

6. **Immutable event log.** Events are never deleted, modified, or re-ordered.
   `sequence_id` is monotonically increasing within a board instance.

7. **Deterministic hydration.** Same Board state produces the same hydrated context
   for a given task. The `context_snapshot_hash` on `TaskRecord` makes this
   verifiable.

8. **Idempotency key stored, not yet enforced.** `idempotency_key` is accepted on all
   mutating intents and persisted in the event log. Deduplication is not yet enforced
   (TODO item 1). Once enforced, duplicate requests with matching key and payload must
   return the cached result; duplicate requests with matching key but different payload
   must raise `ConflictError`.

---

## 10. Backend Contract

Any backend implementing `BoardBackend` must satisfy:

- `create_task` — persist a `TaskRecord` with `UNASSIGNED` status. Raise on duplicate
  `task_id`.
- `update_task` — persist the mutated `TaskRecord`. Must be atomic with respect to
  concurrent callers (use `SELECT ... FOR UPDATE` or equivalent on SQL backends).
- `get_task` — return the `TaskRecord` or `None`. Never raise on missing ID.
- `list_tasks` — return all non-archived tasks. Order is unspecified.
- `upsert_agent` — create or replace the `AgentRecord` for the given `agent_id`.
- `get_agent` — return the `AgentRecord` or `None`.
- `list_agents` — return all registered agents.
- `append_event` — atomically append the `EventRecord` and return its assigned
  `sequence_id`. The sequence must be strictly increasing.
- `list_events_since(sequence_id)` — return all events with `sequence_id` strictly
  greater than the argument, ordered ascending.
- `list_events_for_task(task_id)` — return all events for the task, ordered ascending.
- `list_events_for_agent(agent_id)` — return all events for the agent, ordered
  ascending.
- `put_data(key, value)` — store or overwrite an arbitrary JSON-serialisable value.
- `get_data(key)` — return the stored value or `None`.
- `list_data()` — return all non-internal key-value pairs as a dict. Keys prefixed
  with `_` are internal and must be excluded from this listing.

All integration tests in `tests/integration/` must pass against every backend
implementation.

---

## 11. Versioning

This document covers Quadro v0.1. Changes that add new intents, new event types,
new required fields, or that alter invariant behaviour require a version increment and
a corresponding entry in `CHANGELOG.md`.

Additive changes that do not alter existing contracts (new optional fields, new
backend implementations, new examples) may be made without a version increment.

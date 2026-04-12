# Quadro — Open TODO

For context on what is built, see [`IMPLEMENTATION_ROADMAP.md`](IMPLEMENTATION_ROADMAP.md).

If you pick up an item, open an issue first so work is not duplicated.

Items are grouped by concern. Within each group, priority is noted per item.
Prerequisites are cross-referenced by item number.

---

## Infrastructure

The foundational work that gates real multi-process deployment. Items 1–4 must land
before Quadro can be used outside a single Python process.

---

### 1. Idempotency deduplication

**Priority:** High · **Ref:** M4.2

The `idempotency_key` field is accepted on all mutating intents and persisted in the
event log. Deduplication is not enforced — duplicate requests re-execute the transition.

**What needs to happen:**
- `src/quadro/board/idempotency.py` — `IdempotencyStore` checks keys before execution
- New SQLite table: `idempotency_keys (key PK, fingerprint, result_json, created_at)`
- On key hit with matching fingerprint: return cached result
- On key hit with different fingerprint: raise `ConflictError`
- `QuadroBoard` gains an optional `idempotency_store` parameter

---

### 2. PostgreSQL backend

**Priority:** High

SQLite is single-process. Any multi-worker deployment needs a shared database.
This is the unlock for items 4 and 6.

- `src/quadro/board/backends/postgres.py` implementing `BoardBackend`
- Use `psycopg` v3 with `psycopg_pool.ConnectionPool`
- `SELECT ... FOR UPDATE` on `update_task` to prevent concurrent transition races
- `RETURNING sequence_id` on `append_event`
- All existing integration tests must pass against this backend

---

### 3. MySQL backend

**Priority:** Medium

- `src/quadro/board/backends/mysql.py` implementing `BoardBackend`
- `mysql-connector-python` or `aiomysql`
- `LAST_INSERT_ID()` for sequence ids
- All existing integration tests must pass

---

### 4. Redis backend

**Priority:** Low

For short-lived, high-throughput coordination that does not require durability.

- `src/quadro/board/backends/redis.py` implementing `BoardBackend`
- Hash keys for tasks/agents, sorted set for events scored by sequence id
- Lua script or `WATCH`/`MULTI` for atomic transitions
- Optional TTL on terminal tasks for automatic cleanup

---

### 5. DynamoDB backend

**Priority:** Low

For AWS Lambda / serverless deployments.

- `src/quadro/board/backends/dynamodb.py` implementing `BoardBackend`
- Single-table design: partition key = entity type, sort key for ordering
- Conditional write + atomic counter for `append_event`
- GSI on sequence id for `list_events_since`

---

### 6. A2A network interface abstraction

**Priority:** Medium · **Prerequisite for:** item 7

`BoardClient` is typed to `LocalA2ANetwork` directly. No Protocol or ABC exists for
the network layer, making `HttpA2ANetwork` for multi-process deployments structurally
awkward.

**Fix:** Add a `Protocol` in `a2a/dispatch.py` with `request()` and
`register_endpoint()`. Type `BoardClient` against it. `LocalA2ANetwork` satisfies
it structurally with no changes.

---

### 7. Board as a long-running service

**Priority:** Medium · **Requires:** items 2 and 6

Currently the board lives for the duration of a `RunLoop` call. There is no first-class
concept of a board that persists across multiple runs, accepts tasks from external
callers, and outlives any individual pipeline.

**Idea:** Introduce a `.serve()` or `.live_forever()` mode on `QuadroBoard` that
exposes the board's A2A interface over HTTP and keeps the board alive indefinitely.
External callers (other processes, webhooks, scheduled jobs) can post tasks.
The Chief remains reactive. Workers can be separate processes connecting over HTTP.

---

### 8. Filtered task queries

**Priority:** Medium

`_get_full_state()` returns all tasks on every call. Ombudsman, Chief, and Board UI
all call it; all filtering happens in Python after full deserialisation.

**Fix:** Add `list_tasks_by_status(statuses: set[str]) -> list[TaskRecord]` to
`BoardBackend` ABC. Implement in all backends. Use in `Ombudsman.nudge()`.

---

### 9. Task archival

**Priority:** Medium

Terminal tasks accumulate in the `tasks` table indefinitely. At scale,
`list_tasks()` deserialises all of them on every board read.

**Fix:** Add a `board.archive_task` intent that moves terminal tasks to an
`archived_tasks` table (same schema) and excludes them from `list_tasks()`.
Events are immutable and remain in the event log forever.

---

### 10. `delete_data` board intent

**Priority:** Low

`put_data` and `get_data` exist. There is no way to remove a key from `data_entries`.

**Fix:** Add `board.delete_data` intent, `BoardBackend.delete_data(key)`,
and `BoardClient.delete_data(key)`.

---

## Observability

Tools that make a running system legible — to operators, to tooling, and to the
enterprise stacks that Quadro pipelines sit inside.

---

### 11. OpenTelemetry export

**Priority:** Medium

The Board UI provides a working Kanban view and Chief telemetry panel sufficient for
demos. For production deployments, teams need to route coordination events into their
existing observability stack (Jaeger, Grafana Tempo, Honeycomb, Datadog).

**What to export:**
- Every lifecycle transition as a span: `task_id`, `from_status`, `to_status`,
  `assigned_to`, `duration_ms`
- Chief cycle as a span: `trigger`, `actions_taken`, `cycle_duration_ms`
- Ombudsman ticks: `stale_count`, `statuses_scanned`
- Board data writes are operational and should not emit spans

**Design constraint:** OpenTelemetry must be an optional dependency. The core Board,
Chief, and Worker must remain importable without it. Instrument via an opt-in exporter
class that wraps `BoardBackend` or hooks into the board's event log.

**Why this matters for Quadro specifically:** Unlike frameworks that generate
observability as a side effect of tracing LLM calls, Quadro's event log already
contains the authoritative coordination record. OTel export is a translation of that
record into a format the rest of the enterprise stack understands — not a new source
of truth.

---

### 12. Flight analysis — task trajectory and the flight plan contract

**Priority:** Exploratory

**What was built (the proto-flight-plan)**

The LLM newsroom example already produces a manually assembled flight record for each
article. It captures *what* was produced at each stage but not *how the coordination
worked*: which worker handled each stage, when, how long it sat waiting, whether it was
revised, which Chief cycle dispatched it.

**The proposed contract**

A first-class `BoardClient.flight_plan(task_id)` method that reconstructs the full
trajectory from the event log and merges it with stage outputs stored on the task.

```python
def flight_plan(self, task_id: str) -> dict:
    """
    Returns:
    - task metadata (type, label, priority, created_at, terminal_at)
    - stage log: [{status, entered_at, exited_at, dwell_seconds, assigned_to}]
    - revision count
    - total elapsed seconds
    - raw events from task_history()
    """
```

Workers append domain-specific output via:

```python
def append_flight_log(self, task_id: str, stage: str, payload: dict) -> None:
    """Stored under key `_flight:{task_id}`. No lifecycle event emitted."""
```

**Aggregate analysis**

Once per-task flight plans exist: bottleneck detection, revision rate by task type,
dwell time distribution, stale task concentration by stage.

---

### 13. Chief sleep study

**Priority:** Exploratory

Running the LLM newsroom example reveals interesting patterns in the Chief's sleep and
wake behaviour: sometimes many rapid cycles, sometimes long dormancy, sometimes cycles
that find nothing to do. These patterns reflect the health and rhythm of the pipeline
— but currently the telemetry only shows the last 20 cycle durations.

**Idea:** Build a richer sleep study analysis tool. Log Chief sleep/wake events to a
dedicated table or export. Analyse:
- Sleep duration distribution (is the Chief waking too often? Too rarely?)
- Consecutive noop rate (is it waking to no actionable work — a sign of signal noise?)
- Cycle duration vs pipeline depth (does the Chief slow down as the board fills?)
- Wake trigger breakdown (worker signals vs ombudsman nudges vs seed ticks)
- Correlation between Chief activity patterns and pipeline throughput

The goal is to make the Chief's rhythm legible as a system health indicator.
A healthy pipeline has a recognisable cadence. Anomalies in the sleep pattern
are often the first signal that something is wrong upstream.

Sleep quality variables to model:
- `ChiefState`: sleeping, thinking, or acting
- `InFlightCount`: how many tasks are currently IN_PROGRESS
- `WaitingCount`: how many tasks are UNASSIGNED
- `ResourceAllocation`: ratio of busy workers to waiting tasks, and whether the Chief
  is sleeping while work is waiting (a sign of a dispatch failure)

---

### 23. ~~Bug: Board UI reports noops on every cycle, not just Chief idle wakes~~ ✅ Resolved

**Fixed in:** PR #1 (fix/p0-structural-fixes)

Noops now only increment on reactive worker-triggered wakes where the Chief finds
nothing actionable (signal noise). Seed and ombudsman safety-net nudges that find
nothing are expected behaviour and no longer inflate the counter. The telemetry
test (`test_chief_telemetry_consecutive_noops`) verifies the corrected semantics.

---

## Architecture

Structural decisions that expand what Quadro can model. These are design-heavy,
have open questions, and should not be started without a concrete use case driving
the design.

---

### 14. Board topology — partitioning and multi-board coordination

**Priority:** Exploratory · **Prerequisite for:** items 15 and 17

The current design assumes one board per application run. Two meaningful partition
models worth formalising:

**Domain partitioning.** One board per bounded context (orders, support, content).
Already achievable by running multiple Quadro instances — naming it as a pattern is
the contribution.

**Stage partitioning.** A task terminal on one board seeds a new task on another,
preserving `origin_task_id` for cross-board lineage. Requires an inter-board transfer
protocol. Maps to how real enterprise departments actually hand off work.

Implications: `parent_task_id` field on `TaskRecord`, cross-board flight analysis
(item 12), multi-level Chief sleep study (item 13).

---

### 15. Multi-Chief coordination — domain-scoped coordinators on a shared board

**Priority:** Exploratory · **Requires:** item 14

**The problem this addresses**

As pipelines grow to cover multiple business domains, the work has genuinely different
rhythms, SLAs, and routing logic per domain. A single Chief with a single policy
function can handle this, but it becomes a monolith: every domain's routing logic is
tangled in one place, and a domain that needs real-time reactivity (fraud detection)
runs at the same cadence as one that runs on a daily cycle (financial reconciliation).

**The reference case: insurance claims**

A motor vehicle claim touches four departments in sequence:

1. **Intake** — validate, check policy, acknowledge. Fast, high-volume, needs
   immediate reactivity.
2. **Loss assessment** — dispatch assessor, review photographs, estimate costs.
   Slow, human-in-the-loop, SLA of days not seconds.
3. **Legal/liability** — third-party decision. Weeks. Separate regulatory audit
   trail. Cannot dispatch until assessment is complete.
4. **Payments** — disbursement once approved. Financial controls, runs on a batch
   cycle, not real-time.

These four domains have genuinely different cadences. Forcing them into one Chief
means either the fast domain waits or the slow domain burns unnecessary cycles.

**Two architectural variants**

*Variant A — Domain partition, separate boards (item 14).* Each domain has its own
board, Chief, and workers. A task terminal on one board seeds a new task on the next
with `parent_task_id` preserving lineage. Clean isolation, but cross-domain view
requires joining across event logs.

*Variant B — Shared board, scoped Chiefs.* One board, multiple Chiefs each
responsible for a `task_type` subset. An Intake Chief watches `intake_*` tasks; a
Payments Chief watches `payment_*` tasks. They wake independently on their own
cadences. The full task history across all domains is in one event log.

**The open concurrency question**

Variant B introduces a potential race: two Chiefs waking simultaneously and both
attempting to dispatch the same task. Three candidate solutions:

1. **Rely on atomic board writes.** `update_task` to `IN_PROGRESS` already fails if
   the task is no longer `UNASSIGNED`. The Chief handles this silently today. May
   be sufficient.
2. **Chief domain registration.** Each Chief declares its `task_type` scope at
   registration. `list_tasks()` filters by scope so Chiefs only ever see their own
   domain. Eliminates overlap at the query level.
3. **A Senior Chief.** A meta-coordinator owns dispatch decisions and delegates policy
   evaluation to domain Chiefs. Clean separation, but heavier.

**Open questions before implementation**

- Does Variant B actually give meaningfully different behaviour from Variant A with
  `parent_task_id` lineage, or is the difference mostly cosmetic?
- Is the existing policy seam (`ChiefAgent.builder(bc).policy(fn)`) already
  sufficient for domain-specific routing without multiple Chief instances?
- What does the Board UI show when multiple Chiefs are active? The current telemetry
  panel assumes one Chief.
- Is a Senior Chief pattern just recreating the single-Chief problem at a higher level?

---

### 16. Multi-tenancy and task visibility scoping

**Priority:** Low · **Related to:** items 14 and 15

All tasks on a board are currently visible to all agents. For deployments serving
multiple tenants, departments, or security domains, a `scope` field on `TaskRecord`
would allow the board to filter task visibility at the query level.

**Design:** A `scope` field on `TaskRecord`. `list_tasks()` accepts an optional
`scope` filter. Workers registered with a scope only receive tasks within that scope.
The Chief respects scope boundaries when dispatching.

This is a prerequisite for any serious multi-tenant deployment and is the same
mechanism as Chief domain registration in item 15, approached from the data side
rather than the coordinator side.

---

## Developer experience

Self-contained improvements that can be picked up independently. None of these block
infrastructure work and most can be done in parallel.

---

### 17. YAML / declarative lifecycle configuration

**Priority:** Low

Lifecycle profiles are currently declared in Python using `LifecycleBuilder` or
`lifecycle()`. For teams that want to version lifecycle definitions separately from
application code — or share them across services — a YAML serialisation would be
useful.

```yaml
name: order_fulfilment
transitions:
  - [UNASSIGNED, validating]
  - [validating, validated]
  - [validated, delivering]
  - [delivering, delivered]
revisions:
  - [validating, UNASSIGNED]
```

The loader produces a `Lifecycle` object identical to one built with `LifecycleBuilder`.
The frozen taxonomy and `_expand_custom` logic remain unchanged. YAML is a
serialisation format, not a new runtime model.

Particularly useful for operators who need to audit or approve lifecycle definitions
without reading Python source.

---

### 18. Serialisable pipeline manifest

**Priority:** Low · **Related to:** item 7

There is currently no way to serialise a complete Quadro pipeline — its lifecycle
profiles, worker registrations, and routing policy — into a portable format that can
be versioned, shared, or loaded in a different process.

**Idea:** A `QuadroManifest` that captures the complete pipeline configuration as JSON
or YAML. A second process loads the manifest and reconstructs the pipeline without
duplicating setup code. Especially useful for the long-running board service (item 7)
where workers may be deployed independently.

---

### 19. Scheduled task posting (cron-style)

**Priority:** Low

There is no built-in mechanism for posting tasks on a schedule — a daily report
generation, an hourly inventory check, a recurring compliance audit.

**Idea:** A lightweight `Scheduler` that accepts cron expressions and posts tasks to
the board at the configured interval. The board, Chief, and Workers remain unchanged
— the Scheduler is only a task producer.

```python
scheduler = Scheduler(board_client)
scheduler.every("0 9 * * 1-5").post("compliance_audit", label="Daily audit")
scheduler.run()
```

The board's governed lifecycle applies from the moment the task is posted. Scheduling
is decoupled from coordination — different from Temporal-style systems where the
scheduler and executor are the same component.

---

### 20. `put_data` / `get_data` type annotations

**Priority:** Low

Both `BoardBackend` and `BoardClient` declare `value: dict` but actual stored values
include lists and arbitrary JSON.

**Fix:** Change to `value: Any`.

---

### 21. Revision path integration test

**Priority:** Low · **Ref:** M4.3

`test_revision_cycle.py` exists but a more explicit `test_revision_path.py` walking
the full revision cycle with `assigned_to` audit trail at each phase is missing.

See `IMPLEMENTATION_ROADMAP.md` under M4.3.

---

## Brand & assets

Visual identity work that is independent of runtime code. Safe to run in parallel with
infrastructure and DX items.

---

### 22. New Quadro logo

**Priority:** Low

Replace or refresh the project’s visual mark with a deliberate Quadro identity (pattern
language / multi-agent coordination), not a generic placeholder.

**What needs to happen:**
- Write a short brief: themes to express (coordination, board, agents, clarity at
  small sizes), and what to avoid (busy diagrams, unreadable at 16×16).
- Produce master artwork: vector preferred (SVG) plus raster exports at common sizes
  (e.g. 64, 128, 512 px) for README, docs, and social previews.
- Define usage: light/dark variants if needed, clear space, minimum size.
- Update [`README.md`](README.md) and any other first-touch surfaces that show the logo;
  add or update `assets/` entries and ensure license/attribution for any stock or
  generated elements matches the repo’s [`LICENSE`](LICENSE).

---

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for setup and conventions.

When implementing a new backend:
1. Subclass `BoardBackend` from `src/quadro/board/backends/base.py`
2. Implement all abstract methods — the ABC is the contract
3. All existing integration tests must pass against your backend
4. Add a `conftest.py` fixture that provisions and tears down the backing store
5. Add the backend to `src/quadro/__init__.py` as an optional import

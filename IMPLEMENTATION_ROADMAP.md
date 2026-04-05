# Quadro Implementation Roadmap (v0.1)

## What Quadro is

Quadro is a **pattern language for enterprise multi-agent coordination**, not a
generic agent framework. The code in this repository is a reference implementation
that proves the patterns are coherent and implementable. Read `README.md` before
continuing here.

This roadmap tracks the reference implementation only. The patterns themselves are
described in `README.md` and specified in `QUADRO_SPEC.md`.

---

## Milestone Overview

| Milestone | Focus | Status |
|---|---|---|
| M1 | Board core + lifecycle validator | ✅ Complete |
| M2 | Chief event loop + decision application | ✅ Complete |
| M3 | Worker registration + dispatch | ✅ Complete |
| Track A | Ordering system example + three architectural gaps | ✅ Complete |
| M4.0 | Telemetry query intents | ✅ Complete |
| M4.1 | Ombudsman | ✅ Complete |
| M4.2 | Idempotency deduplication | ⬜ (key persisted; dedup not enforced — v0.1 known gap) |
| M4.3 | Revision path integration test | 🔧 Next |
| M5 | Board UI + observability | ✅ Complete |

---

## M1 — Board Core ✅

### What was built

- `board/records.py` — `TaskRecord`, `AgentRecord`, `EventRecord` dataclasses with
  `TaskStatus` and `AgentStatus` as `StrEnum`
- `board/state_machine.py` — lifecycle profile validator (`review_required`, `fast`)
  with `_expand_with_global` for `FAILED`/`ON_HOLD` transitions
- `board/board.py` — `QuadroBoard` with all mutating and read intents, `_append_event`
  guarded by frozen taxonomy
- `board/backends/base.py` — abstract `BoardBackend`
- `board/backends/sqlite.py` — in-memory and file-backed SQLite backend with inline
  row parsers (no N+1)

### Locked invariants

- Valid transition → persists state → emits exactly one immutable event
- Invalid transition → persists nothing → emits no event
- Every event has monotonically increasing sequence id, timestamp, transition metadata
- `task_heartbeat` stored in event log but classified as `OPERATIONAL_EVENT_TYPES`,
  not `CHIEF_WAKEUP_EVENT_TYPES`

---

## M2 — Chief Loop ✅

### What was built

- `agents/chief.py` — `ChiefAgent` with serialized `nudge()` loop, heartbeat filtering,
  AgentCard-based worker discovery from board registry, and `policy` callback seam
- `agents/hydration.py` — `hydrate_chief_context` and `hydrate_worker_context` with
  deterministic `snapshot_hash`
- `a2a/events.py` — `EventSubscriber` with cursor-based polling

### Locked invariants

- Chief wakes only on `CHIEF_WAKEUP_EVENT_TYPES` (heartbeats filtered before policy)
- Concurrent calls to `nudge()` serialize via `threading.Lock`; `max_concurrent_loops == 1`
- Worker `a2a_url` read from board's `AgentRecord` at dispatch time
- `PENDING_REVIEW → IN_PROGRESS` assigns reviewer as `assigned_to` before dispatch

---

## M3 — Worker Dispatch and Execution Path ✅

### What was built

- `agents/worker.py` — `WorkerAgent` with AgentCard registration, two-argument
  `execute_fn(ctx, board_fn)` signature, heartbeat posting, reviewer mode
- `a2a/dispatch.py` — `LocalA2ANetwork` in-process transport
- `a2a/contracts.py` — frozen intent whitelist, event taxonomy sets, typed envelopes

### Locked invariants

- Workers register via `board.register_agent` with required AgentCard fields
- Workers read task context from board at invocation time (hydration)
- `execute_fn` receives `(context, board_fn)` — operational workers call board intents
  directly via `board_fn`; simple workers ignore the second argument
- Result posting transitions task to the correct next state by profile

---

## Track A — Ordering System Example ✅

### What was built

Three architectural gaps resolved before the ordering example was built:

**Gap 1 — Custom lifecycle profiles**
- `build_custom_profile()` in `state_machine.py` — string-based transition sets
- `validate_transition` accepts optional `custom_profiles` dict
- `QuadroBoard.__init__` accepts `custom_profiles` parameter
- SQLite backend handles status values not in `TaskStatus` enum

**Gap 2 — Board data store**
- `board.put_data` / `board.get_data` intents — arbitrary key-value storage
- `data_entries` table in SQLite backend
- Data entries emit no events
- `board.get_full_state` includes data under `"data"` key

**Gap 3 — Operational worker context**
- `execute_fn(ctx, board_fn)` signature — workers can call board intents during execution
- Backward-compatible: simple workers accept `(ctx, _)` and ignore the second argument
- Worker checks if task was already transitioned by `execute_fn` before calling
  `worker.post_result`

**Ordering system example** (`examples/ordering_system.py`)
- Single file, ~260 lines
- Custom order lifecycle profile (`placed → accepted → awaiting_stock → stock_ready →
  delivering → delivered`)
- Warehouse inventory as board data (not tasks)
- Stock handler uses `board_fn` to read inventory, route conditionally, replenish
  from reserve

### Tests added in Track A

- `tests/unit/test_state_machine.py` — `test_custom_profile_validates_correctly`
- `tests/unit/test_board_data_store.py` — 5 tests
- `tests/integration/test_worker_board_access.py` — 2 tests
- `tests/integration/test_revision_cycle.py` — full revision cycle with reviewer
  rejection and re-assignment

---

## M4.0 — Telemetry Query Intents ✅

The audit trail is already in the `events` table from M1. This milestone adds two
read intents to query it by task or by agent. No schema changes. No new events.
Foundation for the BoardUI in M5 and for execution reports.

### What to build

**`board/backends/base.py`** — two new abstract methods:

```python
@abstractmethod
def list_events_for_task(self, task_id: str) -> list[EventRecord]: ...

@abstractmethod
def list_events_for_agent(self, agent_id: str) -> list[EventRecord]: ...
```

**`board/backends/sqlite.py`** — implement both using `SELECT ... WHERE ... ORDER BY
sequence_id ASC` against the existing `events` table. Parse rows exactly as
`list_events_since` does.

**`a2a/contracts.py`** — add to `ALLOWED_INTENTS`:

```python
"board.get_task_history",
"board.get_agent_activity",
```

**`board/board.py`** — routing and two private methods:

```python
def _get_task_history(self, payload: dict) -> dict:
    # Returns {"task_id": str, "events": list[dict]}

def _get_agent_activity(self, payload: dict) -> dict:
    # Returns {"agent_id": str, "events": list[dict]}
```

### New test file: `tests/unit/test_telemetry_queries.py`

1. `test_get_task_history_returns_only_that_tasks_events` — two tasks, verify filtering
2. `test_get_task_history_includes_heartbeats` — heartbeat events appear in history
3. `test_get_agent_activity_returns_only_that_agents_events` — two agents, verify
   filtering
4. `test_get_task_history_empty_for_unknown_task` — unknown task_id returns empty list,
   not an error

### Acceptance criteria

- Both intents return events in `sequence_id` ascending order
- No cross-task or cross-agent contamination in results
- Unknown IDs return `{"events": []}`, not an error response
- No new tables, no schema changes, no new events emitted
- All 28 existing tests continue to pass

---

## M4.1 — Ombudsman ✅

### What was built

- `ombudsman.py` — `Ombudsman` with configurable `heartbeat_timeout_seconds`.
  Scans `IN_PROGRESS` tasks and transitions stale ones to `STALE` via normal
  board update path.
- `working_statuses` parameter — extends Ombudsman to scan custom-profile statuses
  (e.g. "writing", "researching") and transition stale tasks to `FAILED`.
- `tests/unit/test_ombudsman_custom_statuses.py` — 2 tests covering both paths.
- `tests/integration/test_ombudsman.py` — 4 integration tests.

---

## M4.2 — Idempotency Deduplication ⬜

**New file:** `src/quadro/board/idempotency.py`

```python
class ConflictError(ValueError): ...

class IdempotencyStore:
    def check(self, key: str, fingerprint: str) -> dict | None: ...
    def store(self, key: str, fingerprint: str, result: dict) -> None: ...
```

Fingerprint = `_stable_hash(payload)` from `hydration.py`.

New SQLite table: `idempotency_keys (key PK, fingerprint, result_json, created_at)`.

`QuadroBoard` gains optional `idempotency_store` parameter. When a mutating intent
arrives with `idempotency_key`, check store first; return cached result on hit;
raise `ConflictError` on key collision with different payload.

### New test file: `tests/unit/test_idempotency.py`

1. `test_duplicate_key_same_payload_returns_cached`
2. `test_duplicate_key_different_payload_returns_conflict`
3. `test_no_key_executes_normally`
4. `test_board_without_store_behaves_as_before`

---

## M4.3 — Revision Path Integration Test ⬜

**New test file:** `tests/integration/test_revision_path.py`

No new production code. Explicitly walks a `review_required` task through the full
revision cycle verifying `assigned_to` audit trail at each phase:

```
UNASSIGNED → IN_PROGRESS (writer)
→ PENDING_REVIEW → IN_PROGRESS (reviewer rejects → REVISION_NEEDED)
→ IN_PROGRESS (writer again) → PENDING_REVIEW
→ IN_PROGRESS (reviewer approves) → APPROVED → COMPLETE
```

Note: `test_revision_cycle.py` already covers the basic revision path. This test
is more explicit about the per-phase `assigned_to` audit trail.

---

## M5 — Board UI and Observability ✅

### What was built

- `ui.py` — zero-dependency board UI server (stdlib only, no npm, no React).
  Serves a live Kanban view at `http://localhost:8080`.
- Two usage modes: programmatic (`serve_board(board_client)`) and CLI
  (`python -m quadro.ui path/to/board.db`).
- Live SSE event feed — board updates without polling the page.
- Chief telemetry panel — shows status (thinking/acting/sleeping), cycle count,
  last cycle duration, and a sparkline of recent durations.
- Per-task drawer — click any card to see the full event timeline and output.
- Agent status panel — IDLE/BUSY state with current task ID.
- Board data section — displays non-internal key-value entries.
- Dark/light theme toggle.
- Column order resolved from: explicit arg > `_col_order` board data key >
  event history > current task statuses.

---

## Cross-Cutting Constraints

### Architecture invariants

1. Board is the single source of truth
2. A2A-only boundaries — no direct method calls between board/chief/workers
3. Single transition, single event — data store operations emit no events
4. Deterministic hydration — same snapshot → same hash
5. Chief serialization — one loop at a time
6. Idempotent writes — `idempotency_key` accepted on all mutating task intents

### Scope guardrails

Do not introduce:
- Generic trigger registry or event routing table
- Wildcard event subscriptions
- Non-A2A shortcut paths between components
- Framework-level orchestration replacement

**Now open for contribution** (see `TODO.md`):
- PostgreSQL, MySQL, Redis, and DynamoDB backends
- Idempotency deduplication (M4.2)

### Test conventions

- All integration tests use `LocalA2ANetwork` — no HTTP, no external processes
- Tests interact with the board only through `network.request()` + typed envelopes
- No test calls production code's private methods (prefixed `_`)
- New unit tests in `tests/unit/`, new integration tests in `tests/integration/`

---

## Definition of Done for v0.1

- All milestone acceptance criteria pass (M1 through M4.3)
- All six architecture invariants verifiable through tests
- A2A-only transport policy has no bypasses
- Event taxonomy and lifecycle profiles unchanged from frozen spec
- `README.md`, `QUADRO_SPEC.md`, and this roadmap consistent with each other
- Both example scripts run end-to-end without modification

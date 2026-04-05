# Changelog

All notable changes to this project are documented in this file.

## [0.1.0] — 2026-04-05

Initial public release — reference implementation of the Quadro pattern language.

### What is included

**Core patterns (fully implemented)**
- `QuadroBoard` — durable board with SQLite backend, validated lifecycle
  transitions, and an immutable append-only event log
- `ChiefAgent` — reactive coordinator with pending-wake serialization, chief
  telemetry, and a fluent builder API
- `WorkerAgent` — stateless worker with hydration, heartbeat posting, reviewer
  mode, and a fluent builder API
- `WorkerPool` — fluent builder for pools of workers grouped by capability
- `Ombudsman` — stale heartbeat detection for standard and custom lifecycle profiles
- `LocalA2ANetwork` — in-process A2A transport for testing and single-process use
- `RunLoop` — thin wrapper for coordinated startup and teardown
- `serve_board()` / Board UI — zero-dependency live Kanban at `localhost:8080`

**Lifecycle profiles**
- `review_required` — UNASSIGNED → IN_PROGRESS → PENDING_REVIEW → APPROVED → COMPLETE
- `fast` — UNASSIGNED → IN_PROGRESS → COMPLETE
- Custom profiles via `build_custom_profile()`

**Examples**
- `examples/newsroom_cooperation.py` — multi-agent newsroom with ideation,
  research, writing, and review phases
- `examples/ordering_system.py` — order lifecycle with board data as inventory

**Known gaps (planned for future releases)**
- Idempotency deduplication (key persisted; enforcement not yet active)
- HTTP transport (LocalA2ANetwork only in this release)

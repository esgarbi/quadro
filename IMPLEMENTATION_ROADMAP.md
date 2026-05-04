# Quadro Implementation Roadmap

This roadmap describes the current reference implementation. It is not a public
API contract; it records the architecture that has shipped and the remaining
areas that are intentionally deferred.

## Current Architecture

Quadro is a substrate for governed multi-agent coordination. The core package
owns the Board, lifecycle validation, A2A dispatch, worker/chief coordination,
Sponsor/Lease runtime lifetime, the zero-dependency Board UI, the saga DSL,
and the cost-projection Estimator. It does not own the user's LLM stack.

LLM-framework adapters live outside the substrate in sibling packages:

- `quadro_maf` for Microsoft Agent Framework.
- `quadro_langchain` for LangChain and LangGraph.
- `quadro_anthropic` for the Anthropic SDK (`AnthropicReasoner`). Reasoner-only
  — Anthropic ships an SDK rather than a full agent framework, so there is no
  `AnthropicChiefRuntime`. Install via `pip install "quadro[anthropic]"`.

Those adapters import `quadro`; `quadro` does not import them. The substrate can
run deterministic saga-only pipelines without any LLM framework installed, and
adapter-backed pipelines can register framework runtimes and reasoners through
the existing plugin seams.

## Shipped Foundation

| Area | Status | Notes |
|---|---|---|
| Board core | Complete | Task, agent, event, lifecycle, data-store, and idempotency primitives. |
| A2A dispatch | Complete | Board, chief, and workers communicate through typed A2A envelopes. |
| Chief and workers | Complete | Reactive wakeup, serialized chief cycles, worker registration, and stateless task execution. |
| Sponsor / Lease | Complete | Sponsors are the runtime lifetime model; drain and stop are first-class decisions. |
| Board UI | Complete | Zero-dependency local UI with live event feed, task drawer, chief telemetry, Sponsor panel, and Costs tab. |
| Runtime plugins | Complete | `FrameworkRuntime` protocol and normalized runtime telemetry envelope. |
| Adapter packages | Complete | MAF, LangChain, and Anthropic adapters live outside the substrate. |
| Reporting | Complete | Phase 1 persists per-step token records to the Board under `_token_record:{task_id}:{step_name}`. Phase 2 renders three views in the Board UI: per-card token pill, drawer "Token usage" section, dedicated Costs tab with per-stage stacked bar, optional per-reasoner table, top-tasks-by-cost table, and sponsor budget bar. |
| Estimator + Pricing | Complete | `Estimator.from_dry_run(pipeline, queue, ...)` performs two-pass dry-run sampling for pre-run cost projection. `Estimator.from_history(client, ...)` projects from existing Board records. `runtime.with_pricing({...})` configures dollar attribution; pricing is mirrored to a `_pricing` Board key so SQLite-mode UI invocations can read it without a live runtime. `python -m quadro.estimate <board.db>` is the CLI surface for ad-hoc projections from persisted history. |
| Examples layout | Complete | Examples are purpose-organized (`newsroom`, `ordering`, `ordering_minimal`, `token_budget`, `crm_sponsor`, `cooperation`, `minimal`, `anthropic_minimal`, `estimator`, `synthetic_data`, `workflow_stage_minimal`, `supervisor_stage_minimal`). Each example is self-contained and imports cleanly from one adapter at a time. |

## Saga DSL

The saga DSL rollout is structurally complete for the shipped scope.

### Shipped Step Kinds

- `deterministic`: pure Python work, sync or async.
- `reason`: one LLM reasoning episode through a registered `Reasoner`.
- `gate`: predicate-driven branch selection.
- `guard`: precondition failure with `guard_failed:<step>`.
- `expect`: postcondition failure with `expect_failed:<step>`.
- `evidence`: best-effort audit capture.
- `stamp`: ordered audit marker.
- `parallel`: branch-local concurrent mini-sagas with `all`, `any`, and `n_of_m` joins.

### Shipped Modifiers

- `retry`: typed retry loop with fixed or exponential backoff.
- `deadline`: per-attempt wall-clock timeout.
- `idempotent`: saga-wide idempotency key declaration.

### Shipped Runtime Semantics

- Saga state persists under `_saga:<task_id>`.
- Completed step outputs, evidence, stamps, compensations, reasoner IDs, and
  parallel branch states round-trip through Board data.
- The runtime resumes from persisted `pc` rather than restarting completed work.
- Compensation rollback walks completed steps in reverse order and descends into
  completed parallel branches.
- Deterministic chief mode dispatches saga-only pipelines without an LLM-backed
  chief runtime.
- Runtime telemetry covers saga start/resume/complete, step start/end/failure,
  guard/expect/deadline failures, retry attempts, parallel branch lifecycle, and
  compensation/rollback events.
- Reason-step token usage is captured through the `Reasoner` protocol's
  `token_reporter` callback and persisted as a structured per-step record on
  the Board.

## Documentation

Three guides cover the bulk of the developer-facing surface:

- [`docs/guides/saga-authoring.md`](docs/guides/saga-authoring.md) — the
  starting point for developers writing a new saga. Walks from blank file to
  tested pipeline stage with one section per step kind, modifier, and the
  compensation walkthrough. Includes the deep-agent custom reasoner pattern
  and the project-wide token-usage-in-output convention.
- [`docs/guides/adapters.md`](docs/guides/adapters.md) — the starting point
  for developers writing a new adapter package. Documents the `Reasoner` and
  `FrameworkRuntime` protocols, the sibling-package layout, and two recipes
  (reasoner-only mirroring `quadro_anthropic`, and full-stack mirroring
  `quadro_maf`).
- [`docs/guides/sponsor-decision-matrix.md`](docs/guides/sponsor-decision-matrix.md)
  — the lookup for choosing the right sponsor for a given lifetime
  requirement. Pairs with `docs/guides/sponsor-authoring.md` for users
  writing custom sponsors.

The smallest executable reference is `examples/minimal/`; the compensation
reference is `examples/ordering_minimal/`; the production-shaped LLM
demonstration is `examples/newsroom/`. The cost-projection demonstrations
live at `examples/estimator/` (minimal) and `examples/synthetic_data/`
(production-shaped).

## Deferred Work

### Fork + Join

Milestone F remains deferred. Fork/join is task-level concurrency, not another
branch inside one saga. It likely touches parent/child task relationships,
lifecycle phases for waiting parents, chief routing, persistence, and UI
timeline presentation.

It should not ship until a production-shaped use case constrains the design.
Candidate inputs include multi-document review, batch processing, or
hierarchical task decomposition. Until then, the shipped `parallel` step covers
in-saga concurrency without changing TaskRecord or lifecycle semantics.

### Additional Adapter Packages

`quadro_anthropic` shipped as a first-party adapter (see Shipped Foundation
above). Further adapters remain future work, dependent on a concrete user
need justifying first-party maintenance. The substrate already supports
user-authored adapters through the `Reasoner` and `FrameworkRuntime` seams.
Candidates under active discussion are catalogued in
[`docs/guides/adapters.md`](docs/guides/adapters.md#future-candidates) —
LiteLLM (a single-client proxy over ~100 providers), native AWS Bedrock,
native Google Vertex AI, and a community LlamaIndex adapter.

### Board UI Saga Timelines

The backend emits enough saga telemetry to build a timeline view, but the UI
has not grown a dedicated saga visualization. That is a UI project, not a
blocker for the DSL runtime.

### Declarative Saga Files

TOML/YAML saga authoring and validation remain future possibilities. The
current Python builder is the supported authoring surface. (Lifecycle
profiles already support TOML files via `quadro.load_lifecycle()`; the
saga-DSL extension would be the analogous shape one level down.)

### Distributed Execution

The current reference implementation uses in-process A2A dispatch for examples
and tests. Distributed worker nodes, remote transports, and production backend
packages are contribution areas rather than saga rollout prerequisites.

### Reporting Phase 3 — CLI report sub-command

Folded into the Estimator milestone. `python -m quadro.estimate <board.db>`
prints a token-and-dollar report from a board's persisted history and
optionally projects forward to N tasks, which covers the original phase-3
intent. A separate sub-command may still be added if a real consumer asks
for a different shape.

### Reporting Phase 4 — explicit non-goals

Templating engines (Jinja2/Mako/etc.), separate metrics databases, scheduled
report emails, webhooks-out reporting bridges, dedicated Prometheus exporters
beyond the small `quadro_llm_tokens_total` counter Phase 2 added (use
`quadro.integrations.otel` for richer needs), CSV export features, and
pluggable reporting backends are explicit non-goals. Each thread pulls
Quadro toward being-a-reporting-tool. The substrate stays focused on
governed coordination; reporting backends are downstream of
`BoardClient.token_records()` and the public `Estimator` API.

## Invariants

These properties should continue to hold:

- The Board is the single source of truth.
- Component boundaries use A2A request envelopes.
- Lifecycle transitions emit exactly one immutable event.
- Board data writes do not emit task lifecycle events.
- The substrate has no LLM-framework imports.
- `Reasoner` and `FrameworkRuntime` stay framework-neutral.
- New adapters live outside `src/quadro/`.
- Existing saga step kinds and modifiers are extended by telemetry and bug
  fixes, not by hidden protocol changes.
- Pricing is configured at the runtime level and mirrored to the Board so
  SQLite-mode UI invocations resolve dollar amounts without a live runtime.

## Release Health Checks

These criteria reflect the properties that hold at every shipping release.
They are written prospectively — a future maintainer checking the same boxes
should still find the substrate in this shape.

- All existing tests pass.
- The substrate-purity import grep returns zero matches:

  ```bash
  grep -rE "^(import|from) (agent_framework|langchain|anthropic|openai)" src/quadro/
  ```

- `examples/minimal/` demonstrates a custom reasoner with no framework adapter.
- `examples/newsroom/` runs through MAF adapter composition.
- `examples/ordering_minimal/` demonstrates compensation rollback.
- `examples/anthropic_minimal/` demonstrates the reasoner-only adapter shape
  and the project-wide token-usage-in-output convention.
- `examples/estimator/` produces a calibrated cost projection with a
  bounded sample run cost.
- The Costs tab renders dollar columns when pricing is configured on the
  runtime, and gracefully hides them when it is not.
- [`docs/guides/saga-authoring.md`](docs/guides/saga-authoring.md) is current
  with the builder and runtime.
- [`docs/guides/adapters.md`](docs/guides/adapters.md) matches the current
  substrate model — `Reasoner` and `FrameworkRuntime` protocols, sibling
  package layout, three reference implementations.
- Fork/join remains explicitly deferred until a real use case appears.

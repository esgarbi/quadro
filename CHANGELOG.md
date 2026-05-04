# Changelog

## v0.8.0 — 2026-05-03

The first release after the saga DSL rollout, the substrate rewrite,
the examples reorganization, the four-phase reporting rollout (phases
1+2 shipped; 3 folded into the Estimator; 4 explicit non-goals), and
two cost-side milestones — the Anthropic adapter and the
cost-projection Estimator. The substrate is now framework-neutral by
construction: `src/quadro/` has zero LLM-framework imports, and three
sibling adapter packages (`quadro_maf`, `quadro_langchain`,
`quadro_anthropic`) implement the `Reasoner` and `FrameworkRuntime`
protocols. A 30-line bare-OpenAI reference adapter at
`examples/minimal/openai_reasoner.py` proves the plug-in story.

### Breaking changes

- **Adapter packages live as siblings, not under
  `quadro.integrations`.** `quadro.integrations.maf` and
  `quadro.integrations.langchain` are gone. Imports move to
  `quadro_maf` and `quadro_langchain` (sibling top-level packages
  installed via the `[maf]` and `[langchain]` optional extras).
  `quadro.integrations.otel` remains in the substrate as the only
  LLM-framework-free integration.
- **`Pipeline` subclassing is replaced by protocol composition.** The
  pre-J1 hooks (`_make_stage_spec`, `_decorate_tools`,
  `_run_chief_llm_turn`, `_make_auto_execute_fn`) no longer exist.
  Adapters now implement the `Reasoner` protocol and (optionally) the
  `FrameworkRuntime` protocol from `src/quadro/runtime_plugins/base.py`,
  registered via `Pipeline.reasoner(...)` and
  `Pipeline.with_framework_runtime(...)`. See
  [`docs/guides/adapters.md`](docs/guides/adapters.md).
- **Examples reorganized into purpose-based flat folders.** Old paths
  under `examples/microsoft_agent_framework/` and `examples/langchain/`
  no longer exist. Examples now live at
  `examples/newsroom/`, `examples/ordering/`,
  `examples/ordering_minimal/`, `examples/token_budget/`,
  `examples/cooperation/`, `examples/crm_sponsor/`,
  `examples/minimal/`, `examples/anthropic_minimal/`,
  `examples/estimator/`, `examples/synthetic_data/`,
  `examples/workflow_stage_minimal/`,
  `examples/supervisor_stage_minimal/`. Each example imports
  cleanly from one adapter at a time; polyglot contamination is
  explicitly rejected.
- **`Reasoner` protocol gained an additive optional `step_name` kwarg.**
  Existing user-defined reasoners that accept `**kwargs` continue to
  work unchanged. Reasoners that explicitly enumerate parameters need
  a one-line signature update; the saga runtime always passes the
  kwarg, and the three shipping adapters were updated in lockstep.

### Added — Substrate model (J1)

- `src/quadro/` no longer imports any LLM framework. The substrate
  package owns the Board, the governed lifecycle, the Chief, the
  WorkerPool, the Sponsor model, the Saga DSL, the deterministic
  Saga runtime, and the Board UI — and nothing else.
- Adapter packages (`quadro_maf`, `quadro_langchain`,
  `quadro_anthropic`) live as sibling top-level packages under
  `src/`. Each is installable via an optional extra
  (`pip install "quadro[maf]"`, etc.). Adapter packages import
  `quadro`; `quadro` does not import them.
- `Pipeline.reasoner(...)` registers a Reasoner for `reason` saga
  steps. `Pipeline.with_framework_runtime(...)` registers a
  `FrameworkRuntime` for stage-level integration (chief tooling,
  native stage paths like `stage(workflow=...)` /
  `stage(supervisor=...)`).
- `FrameworkRuntime` protocol at
  `src/quadro/runtime_plugins/base.py` formalises the
  framework-adapter seam: `can_handle`, `decorate_tools`,
  `run_chief_turn`, `run_stage`. Replaces the pre-J1 Pipeline-subclass
  hooks.

### Added — Examples reorganization (J2)

- Examples are organized by what they teach, not by which LLM
  framework they use. Each folder is self-contained — copy any
  example as the starting point for a new pipeline.
- New `examples/minimal/` example demonstrates the substrate's
  plug-in story with a 30-line bare-OpenAI-SDK reasoner adapter
  (`examples/minimal/openai_reasoner.py`). The same shape works for
  Google, LiteLLM, in-house frameworks, or any SDK that can fulfill
  prompt-in / response-out.
- `examples/README.md` documents what each example teaches and
  which adapter it uses.

### Added — Saga DSL extensions

Three saga DSL extension milestones shipped between v0.2 and v0.8:

- **Compensation rollback (milestone D).** `.compensate(step,
  undo=...)` registers an undo function for a completed step. When
  a later step fails, the runtime walks completed steps in reverse
  insertion order and invokes each registered compensation. Default
  failure mode is `continue` (best-effort); `on_failure="halt"`
  stops the walker on first compensation failure. Four new
  telemetry event types — `saga.compensation_start`,
  `saga.compensation_end`, `saga.compensation_failed`,
  `saga.rollback_complete`. The `examples/ordering/` example is the
  headline demonstration; pass `--inject-failure <step>` to
  exercise the walker without needing a real upstream failure.
- **Parallel branches (milestone E).** `.parallel(name, join=...,
  branches=[...])` runs concurrent branch-local mini-sagas with
  three join modes: `all` (waits for every branch), `any`
  (continues on first success and cancels the rest), `("n_of_m", n)`
  (waits for a quorum). Compensation rollback walks completed
  branches' compensations in reverse; cancelled branches do not
  fire compensations because they did not finish their side
  effects.
- **LangChain reasoner (milestone G).** `quadro_langchain` ships
  `LangChainReasoner` and `LangChainChiefRuntime` for LangChain /
  LangGraph integration, alongside the pre-existing `quadro_maf`
  adapter. Polyglot reasoning is supported via `via="reasoner_id"`
  on individual `reason` steps when multiple reasoners are
  registered.

### Added — quadro_anthropic adapter

- New sibling package `quadro_anthropic` ships an `AnthropicReasoner`
  for the Anthropic SDK. Reasoner-only — Anthropic ships an SDK
  rather than a full agent framework, so there is no
  `AnthropicChiefRuntime`; use `stage(execute_fn=...)` for
  Claude-driven chief logic.
- Default model `claude-sonnet-4-6`. JSON-mode schema validation
  via system-prompt augmentation (Anthropic's API has no
  OpenAI-style `response_format`); defensive markdown-fence
  stripping handles the "Claude wrapped the JSON in code fences"
  failure mode.
- Token attribution flows through phase-1 records automatically:
  every reason step using `AnthropicReasoner` produces a
  `_token_record:{task_id}:{step_name}` Board entry the same shape
  as MAF and LangChain reasoners. No adapter-specific telemetry
  code was needed.
- New `examples/anthropic_minimal/` folder is the smallest example
  using Claude as the reasoner — and the reference implementation
  for the project-wide token-usage-in-output convention. The
  `_format_tokens` and `_print_token_usage` helpers there are
  copy-paste-ready for any new example.

### Added — Reporting (Phases 1 + 2)

**Phase 1 — token records on the Board.** Every successful reason
step persists a structured per-step token record under the
`_token_record:{task_id}:{step_name}` key in the Board's data store.
Records carry `task_id`, `stage`, `step_name`, `reasoner_id`,
`token_total`, `token_prompt`, `token_completion`, and `timestamp`.
Three new aggregator methods on `BoardClient`: `token_records()`,
`tokens_by_stage()`, `tokens_by_reasoner()`. `BoardClient.list_data`
gained an optional `prefix` parameter for prefix-filtered reads.

**Phase 2 — visualization layer in the Board UI.** The Costs tab,
sitting alongside the Kanban view, renders three views of phase 1's
data:

- A per-card running-total token pill on every Kanban card with
  recorded usage.
- A "Token usage" section in the existing task drawer with a
  prominent total + per-step breakdown table (step, stage,
  reasoner, tokens).
- A dedicated **Costs** tab in the header strip showing a headline
  strip (TOTAL TOKENS, TASKS WITH RECORDS, AVG PER TASK), a
  per-stage stacked bar with inline labels, a per-stage breakdown
  table, an optional per-reasoner table (hidden when only one
  reasoner has records), a top-tasks-by-cost table with
  click-through to the task drawer, and a sponsor budget bar
  rendered when an `LlmTokenBudgetSponsor` is configured. All
  stdlib HTML/CSS/JS, zero-dependency-UI discipline preserved.

The four-phase rollout's phases 3 (CLI report) and 4 (templating /
metrics DB / scheduled emails) are explicit non-goals; phase 3
ultimately folded into the Estimator's `python -m quadro.estimate`
CLI.

### Added — Estimator and Pricing

- **`quadro.Estimator`** projects token and dollar costs for running
  a saga against a queue of tasks. `Estimator.from_dry_run(pipeline,
  queue, *, max_sample_cost_dollars=1.0, max_samples=8,
  confidence=0.95)` performs a two-pass dry run: pass 1 walks every
  task in the queue with a `CollectingReasoner` to characterise
  input shapes; pass 2 samples representative tasks (sorted across
  the input-size distribution so the sample spans the variation the
  queue contains) through the real reasoner. The result is a
  `Projection` with mean cost, a 95% confidence interval, per-stage
  breakdown, and a coefficient-of-variation warning when inputs are
  heterogeneous enough that the estimate is genuinely uncertain.
- **`Estimator.from_history(client, *, pricing=None,
  confidence=0.95)`** projects from existing Board token records,
  for when you've already executed a slice of work and want to
  project the rest from real per-task costs.
- **`Estimator.format()`** renders a human-readable projection
  report. **`Estimator.project(n_tasks=...)`** returns a `Projection`
  for a different task count than the calibrated default.
- **`runtime.with_pricing({...})`** configures dollar projection at
  the runtime level. Pricing is mirrored to a `_pricing` Board data
  key so SQLite-mode UI invocations can read pricing without a
  live runtime. Pricing flows through to both the Estimator's
  projections and the Costs tab's dollar columns.
- **`python -m quadro.estimate <board.db>`** CLI projects historical
  cost from a board's persisted records. Accepts `--project-tasks N`,
  `--pricing-file path.json`, and `--confidence 0.95`.
- **Costs tab dollar integration.** When pricing is configured,
  every Costs tab table gains a `$` column alongside the Tokens
  column. The TOTAL TOKENS and AVG PER TASK headline tiles render
  inline `$` sub-amounts. A small ⓘ tooltip on the TOTAL TOKENS
  sub-line documents the pricing source, model rates, the assumed
  `io_ratio`, and a verification URL. When pricing is not
  configured, all dollar elements are hidden — graceful
  degradation matching the phase-2 pattern.
- **`examples/estimator/`** is the minimal demonstration: a 50-task
  translation queue, sampled under a `$1.00` cap, with a
  token-and-dollar projection.
- **`examples/synthetic_data/`** is the production-shaped
  demonstration: HuggingFace `Salesforce/wikitext`
  `wikitext-103-raw-v1` passages run through two distinct sagas
  (SQuAD-style extractive QA and Alpaca-style multi-hop reasoning
  chains with chain-of-thought traces), surfacing per-saga cost
  asymmetry and projecting against full-scale workloads. Outputs
  JSONL files in formats directly loadable by the HuggingFace
  `datasets` library. Defaults `--passages 50`,
  `--scale-passages 5000`. Example-local extras live in the
  folder's `requirements.txt`.

### Fixed — Polish (H)

- The spurious `RuntimeWarning: coroutine '_chief_policy' was never
  awaited` at the end of newsroom runs is gone.
- The newsroom example's end-of-run "Articles (N):" listing now
  counts only the current run's output rather than reading every
  `.md` in `output/` and `output_sample/`.
- The deterministic chief no longer logs the misleading
  `GOAL_REACHED` line during normal operation.
- `quadro_maf`'s public API trimmed: `make_auto_execute_fn` was
  removed from `__all__` (it has no consumer in any shipping
  example; the saga DSL has made saga-driven stages the dominant
  pattern).
- `src/quadro/integrations/__init__.py` docstring now correctly
  describes the package as the OpenTelemetry-only home, not
  "external agent framework" adapters.

### Fixed — Estimator math correction (post-shipping)

- The prediction-interval formula in `Projector` was corrected from
  `margin = t × stdev × sqrt(N)` to `margin = t × stdev × sqrt(N +
  N²/n)`. The original formula was the standard-error-of-the-sum
  formula assuming known population parameters — but in practice
  the mean and stdev are estimated from the same `n` calibration
  samples, and that estimate has its own uncertainty. The corrected
  formula compounds per-task variability with parameter-estimate
  uncertainty, widening confidence intervals visibly when
  extrapolating from small samples to large queues. A regression
  test (`test_projection_widens_ci_when_extrapolating_far_beyond_sample_size`)
  now asserts that relative CI width does not collapse to near zero
  on small-sample-to-large-N projections.

### Documentation

- `README.md` rewritten end-to-end. New sections for the Estimator,
  the three sibling adapter packages, the Costs tab + dollar
  integration, and the Realtime usage Board UI. Examples section
  reorganized to match the J2 flat layout.
- `CONTRIBUTING.md` updated for the new examples paths.
- `docs/guides/saga-authoring.md` is the full saga authoring
  reference — one section per step kind, modifier explanations,
  compensation walkthrough, the deep-agent custom reasoner
  pattern, the project-wide token-usage-in-output convention,
  testing patterns, and a production checklist.
- `docs/guides/adapters.md` rewritten end-to-end against the
  current substrate model (Reasoner + FrameworkRuntime protocols,
  `Pipeline.reasoner()` / `Pipeline.with_framework_runtime()`
  composition, sibling-package layout, three reference
  implementations).
- `IMPLEMENTATION_ROADMAP.md` rewritten around the substrate
  model. `quadro_anthropic` moved from Deferred to Shipped.
- `QUADRO_SPEC.md` updated to v0.8.
- Every example folder has a `README.md`. Each example's README
  documents what it teaches, what it requires (extras + API key),
  and how to run it.

## v0.2.0 — 2026-04-25

### Breaking changes

- **Lifetime model replaced by Sponsor/Lease.** `QuadroRuntime.done_when`,
  `QuadroRuntime.max_cycles`, `RunLoop.done_when`, and
  `RunLoop.max_cycles` are removed. Runtimes must now install a
  :class:`~quadro.sponsor.Sponsor` via `.sponsor(sponsor)`.
  `GoalSponsor(predicate)` is the canonical drop-in replacement for
  `done_when(predicate)`; `TickBudgetSponsor(n)` replaces `max_cycles(n)`.
  See [docs/guides/sponsor-migration.md](docs/guides/sponsor-migration.md).
- **`BuiltPipeline.run(done_when=..., max_cycles=...)` removed.** Route
  built pipelines through `QuadroRuntime.run(pipeline)` instead. Examples
  under `examples/microsoft_agent_framework/*/main_pipeline.py` show the
  new shape.

### Added

- `quadro.sponsor` — new module exposing:
  - :class:`Sponsor` protocol, :class:`Lease`, :class:`LeaseDecision`
    union (`Continue` / `Drain` / `Stop`), :class:`SponsorContext`,
    :class:`MeterReadings`.
  - Leaf sponsors: :class:`GoalSponsor`, :class:`DeadlineSponsor`,
    :class:`TickBudgetSponsor`, :class:`WorkerBudgetSponsor`,
    :class:`LlmTokenBudgetSponsor`, :class:`BoardEventBudgetSponsor`,
    :class:`CallableSponsor`, :class:`QueueDepthSponsor`.
  - External sponsors: :class:`HttpSponsor`, :class:`CallbackSponsor`.
  - Composers: :class:`AllOf`, :class:`AnyOf`, :class:`Priority`.
  - Test fixtures: :class:`AlwaysOnSponsor`, :class:`AlwaysStopSponsor`,
    :class:`ScriptedSponsor`.
- Drain semantics are first-class. `Drain(deadline, reason)` suppresses
  new task assignment while letting in-flight work complete. The runtime
  publishes a drain flag at `_runtime_draining` and a status snapshot at
  `_sponsor_status`.
- Observability: `_sponsor_log` (bounded list of recent decisions) and
  `_sponsor_status` (active lease + draining flag) are persisted to the
  board and surfaced in the UI sidebar as a new **Sponsor** panel.
- `QuadroBoard.add_event_listener` / `remove_event_listener` — subscribe
  to every board event (consumed by `BoardEventMeter`).
- `ChiefAgent.add_wake_listener` / `remove_wake_listener` — subscribe to
  every chief wake (consumed by `WorkerInvocationMeter`).
- `ChiefAgent.set_draining(bool)` and `is_draining()` — drain flag with
  board-published telemetry.
- `quadro.dispatch.is_draining(board_fn)` and `DRAIN_FLAG_KEY` —
  dispatch helpers consult the flag so custom chief policies cooperate
  without per-call-site changes.
- `QuadroRuntime.drain_max_duration(td)` — override the 5-minute default
  fallback deadline used when a Sponsor returns `Drain(deadline=None)`.
- New example `examples/core/crm_sponsor/` — mocked CRM ticket drives a
  runtime's lifetime, showing the continuity story end-to-end.
- `examples/microsoft_agent_framework/llm_token_budget/` — MAF-backed ticket triage gated by an
  `AllOf(QueueDepthSponsor, LlmTokenBudgetSponsor, DeadlineSponsor)`
  composition. Includes a sample report from a real run at two budget
  ceilings. The headline demonstration of token-cost governance.

### Documentation

- `docs/design/sponsor.md` — design doc with locked API and open-question
  answers.
- `docs/guides/sponsor-authoring.md` — how to write a Sponsor.
- `docs/guides/sponsor-decision-matrix.md` — "I want X, use Y" lookup.
